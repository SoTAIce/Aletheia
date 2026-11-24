"""Citation checking, task-wide renumbering, CLI/archive serialization, and D-033 (D-098)."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from loguru import logger as loguru_logger

from app.agent.__main__ import report, result_record
from app.agent.agent_loop import ASK_USER, AgentLoopRunner, LoopOptions, LoopResumeStore
from app.agent.citations import (
    CitationLedger,
    citation_repair_instruction,
    cited_refs,
)
from app.agent.delegation import (
    DELEGATE,
    DelegatingLoopRunner,
    DelegationOptions,
    ExploreLoopRunner,
    ExploreOptions,
)
from app.agent.hooks import HookSet, using_hooks
from app.agent.loop_run import LoopRun
from app.agent.output_contract import OutputContract
from app.agent.runtime import AgentRunResult
from app.agent.step_models import (
    ModelTurn,
    StepOutcome,
    SystemMessage,
    ToolCall,
    ToolDefinition,
    ToolOutput,
    ToolsShownOnly,
)
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.agent.trace_view import build_trace, render_text
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.schemas.rag import Citation, RetrievedDocument
from app.services.rag_agent_service import RagAgentService, _restore_result
from app.services.rag_context_builder import ContextBuilder
from app.services.rag_reranker_service import RerankerService
from app.tools.knowledge_registry_tool import KnowledgeRegistryTool
from app.tools import knowledge_tool
from app.tools.tool_registry import ToolRegistry
from loop_fakes import started_context


class ScriptedModel:
    """Returns scripted ModelTurns (or raises exceptions) while recording requests."""

    def __init__(self, *turns) -> None:
        self._turns = list(turns)
        self.requests: list[tuple[tuple, object]] = []

    async def generate(self, messages, tools=()):
        self.requests.append((tuple(messages), tools))
        nxt = self._turns.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _build_evidence(docs: list[Document]) -> tuple[str, dict]:
    ranked = [RetrievedDocument(document=d, distance=0.1 * i) for i, d in enumerate(docs, 1)]
    built = ContextBuilder(token_budget=4000).build(ranked, final_top_k=len(docs))
    return built.context, {
        "status": "ok",
        "documents": built.selected_documents,
        "error": None,
        "trace": None,
        "citations": built.citations,
    }


def _ctx(goal: str = "question"):
    return started_context(RunMemory(RunState("u1", goal)))


def _doc(text: str, *, file_name: str = "guide.md", source: str = "docs/guide.md",
         h1: str = "Intro", start_line: int | None = 1, end_line: int | None = 10) -> Document:
    meta = {"_file_name": file_name, "_source": source, "h1": h1}
    if start_line is not None:
        meta["start_line"] = start_line
    if end_line is not None:
        meta["end_line"] = end_line
    return Document(page_content=text, metadata=meta)


# ---- Parsing & Ledger unit tests ----

def test_cited_refs_recognizes_single_comma_spaced_and_adjacent():
    text = "See [ref:2], also [ref:1,3] and [ref:3, 4][ref:2][ref:5]."
    assert cited_refs(text) == [2, 1, 3, 4, 5]
    assert cited_refs("No citations here.") == []


def test_repair_instruction_lists_refs_and_uses_range_above_30():
    short = citation_repair_instruction([9], {1, 2, 3})
    assert "[ref:9]" in short and "1, 2, 3" in short and "Do not call any tools" in short

    wide = citation_repair_instruction([99], set(range(1, 36)))
    assert "[ref:99]" in wide and "1-35" in wide

    empty = citation_repair_instruction([1], set())
    assert "[ref:1]" in empty and "No knowledge base references" in empty


# ---- Renumbering in KnowledgeRegistryTool ----

@pytest.mark.asyncio
async def test_renumbering_continues_across_calls_reuses_duplicates_and_keeps_body_text():
    memory = RunMemory(RunState("u1", "question"))
    d1 = _doc("First fact mentioning [ref:9] inline.\n[ref:9]\nEnd.", file_name="a.md", source="a.md", start_line=1, end_line=3)
    d2 = _doc("Second fact.", file_name="b.md", source="b.md", start_line=10, end_line=12)
    d3 = _doc("Third fact.", file_name="c.md", source="c.md", start_line=20, end_line=25)
    batches = [[d1, d2], [d2, d3]]

    async def fake_search(query, history):
        return _build_evidence(batches.pop(0))

    tool = KnowledgeRegistryTool(memory, search=fake_search)
    out1 = await tool.invoke({"query": "first"})
    out2 = await tool.invoke({"query": "second"})

    p1 = json.loads(out1.content)
    p2 = json.loads(out2.content)
    assert [c["ref_id"] for c in p1["citations"]] == [1, 2]
    assert "[ref:1]\nfile: a.md" in p1["evidence"]
    assert "[ref:2]\nfile: b.md" in p1["evidence"]
    # Literal [ref:9] in body text (both inline and standalone line) stays untouched.
    assert "mentioning [ref:9] inline.\n[ref:9]\nEnd." in p1["evidence"]

    # Second call: d2 reuses 2, d3 gets 3.
    assert [c["ref_id"] for c in p2["citations"]] == [2, 3]
    assert "[ref:2]\nfile: b.md" in p2["evidence"]
    assert "[ref:3]\nfile: c.md" in p2["evidence"]
    assert memory.citations.valid() == {1, 2, 3}
    assert memory.citations.entry(3)["file_name"] == "c.md"
    assert memory.citations.entry(3)["snippet"] == "Third fact."


@pytest.mark.asyncio
async def test_renumbering_mismatch_keeps_evidence_and_registers_raw(caplog):
    memory = RunMemory(RunState("u1", "question"))

    async def fake_search(query, history):
        c1 = Citation(ref_id=1, source="a.md", file_name="a.md", heading_path="", start_line=1, end_line=2, distance=0.1)
        c2 = Citation(ref_id=2, source="b.md", file_name="b.md", heading_path="", start_line=3, end_line=4, distance=0.2)
        # Only one header block for two citations -> mismatch.
        return "[ref:1]\nfile: a.md\nsource: a.md\nheading: \ncontent:\nonly one", {
            "status": "ok", "documents": [], "error": None, "trace": None, "citations": [c1, c2],
        }

    tool = KnowledgeRegistryTool(memory, search=fake_search)
    with caplog.at_level("WARNING"):
        out = await tool.invoke({"query": "q"})
    payload = json.loads(out.content)
    assert payload["evidence"].startswith("[ref:1]\n")
    assert [c["ref_id"] for c in payload["citations"]] == [1, 2]
    assert memory.citations.valid() == {1, 2}


# ---- Pre-delivery citation check in AgentLoopRunner ----

@pytest.mark.asyncio
async def test_no_citations_in_answer_skips_check_and_emits_no_event():
    events = []
    registry = ToolRegistry()
    model = ScriptedModel(ModelTurn(content="Plain answer without refs."))
    runner = AgentLoopRunner(model, registry)
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        output = await runner.run(_ctx("plain"))
    assert output.content == "Plain answer without refs."
    assert output.citations == ()
    assert output.citation_problems == ()
    assert not any(e.get("type") == "citation_check" for e in events)
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_valid_citations_pass_and_populate_result_without_scores():
    memory = RunMemory(RunState("u1", "check"))
    long_text = "X" * 600
    d1 = _doc(long_text, file_name="spec.md", source="docs/spec.md", h1="Rules", start_line=5, end_line=20)
    d2 = _doc("Second snippet.", file_name="faq.md", source="docs/faq.md", h1="FAQ", start_line=1, end_line=4)

    async def fake_search(query, history):
        return _build_evidence([d1, d2])

    registry = ToolRegistry()
    registry.register(KnowledgeRegistryTool(memory, search=fake_search))
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall("c1", "retrieve_knowledge", {"query": "rules"}),)),
        ModelTurn(content="Rule B [ref:2] and Rule A [ref:1,2]."),
    )
    events = []
    runner = AgentLoopRunner(model, registry, citation_ledger=memory.citations)
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        output = await runner.run(_ctx("rules"))

    assert output.citation_problems == ()
    assert [c["ref"] for c in output.citations] == [2, 1]
    assert output.citations[0]["file_name"] == "faq.md"
    assert output.citations[1]["file_name"] == "spec.md"
    assert len(output.citations[1]["snippet"]) == 500
    assert set(output.citations[0]) == {
        "ref", "file_name", "source", "heading_path", "start_line", "end_line", "snippet"
    }
    cite_events = [e for e in events if e.get("type") == "citation_check"]
    assert len(cite_events) == 1
    assert cite_events[0]["result"] == "pass"
    assert cite_events[0]["cited"] == 2 and cite_events[0]["invalid"] == 0


@pytest.mark.asyncio
async def test_invalid_citation_repairs_once_and_passes():
    memory = RunMemory(RunState("u1", "q"))
    d1 = _doc("Secret snippet text ABC123", file_name="a.md", source="a.md", start_line=1, end_line=5)

    async def fake_search(query, history):
        return _build_evidence([d1])

    registry = ToolRegistry()
    registry.register(KnowledgeRegistryTool(memory, search=fake_search))
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall("c1", "retrieve_knowledge", {"query": "q"}),)),
        ModelTurn(content="Answer with bad ref [ref:9]."),
        ModelTurn(content="Answer with valid ref [ref:1]."),
    )
    events = []
    runner = AgentLoopRunner(model, registry, citation_ledger=memory.citations)
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        output = await runner.run(_ctx("q"))

    assert output.content == "Answer with valid ref [ref:1]."
    assert [c["ref"] for c in output.citations] == [1]
    assert output.citation_problems == ()
    assert runner.format_repair_count == 1
    # Repair turn must not allow tools (ToolsShownOnly).
    assert isinstance(model.requests[-1][1], ToolsShownOnly)
    assert isinstance(model.requests[-1][0][-1], SystemMessage)
    assert "[ref:9]" in model.requests[-1][0][-1].content
    cite_events = [e for e in events if e.get("type") == "citation_check"]
    assert [e["result"] for e in cite_events] == ["repair", "pass"]
    # Neither answer text nor snippet leaks into hook events.
    raw = json.dumps(events, ensure_ascii=False)
    assert "Secret snippet text ABC123" not in raw
    assert "Answer with" not in raw


@pytest.mark.asyncio
async def test_invalid_citation_after_repair_delivers_flagged_as_completed():
    memory = RunMemory(RunState("u1", "q"))
    registry = ToolRegistry()
    model = ScriptedModel(
        ModelTurn(content="First bad [ref:7]."),
        ModelTurn(content="Still bad [ref:8]."),
    )
    events = []
    run = LoopRun(
        memory, model, registry,
        runner_factory=lambda deps: AgentLoopRunner(
            deps.model, deps.tool_registry, citation_ledger=memory.citations,
        ),
        step_description="test",
    )
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        result = await run.run()

    assert result.status is TaskStatus.COMPLETED
    assert result.content == "Still bad [ref:8]."
    assert result.citations == ()
    assert result.citation_problems == (8,)
    cite_events = [e for e in events if e.get("type") == "citation_check"]
    assert [e["result"] for e in cite_events] == ["repair", "flagged"]


@pytest.mark.asyncio
async def test_tool_call_during_citation_repair_is_refused_and_delivers_previous_answer():
    called = []
    registry = ToolRegistry()

    async def invoke(args):
        called.append(args)
        return ToolOutput(True, "should not run")

    registry.register(SimpleNamespace(
        definition=ToolDefinition("read_file", "read", {"type": "object"}, effect="read"),
        invoke=invoke,
    ))
    model = ScriptedModel(
        ModelTurn(content="Original answer [ref:42]."),
        ModelTurn(tool_calls=(ToolCall("c1", "read_file", {}),)),
    )
    events = []
    runner = AgentLoopRunner(model, registry)
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        output = await runner.run(_ctx("q"))

    assert called == []
    assert output.outcome is StepOutcome.SUCCEEDED
    assert output.content == "Original answer [ref:42]."
    assert output.citation_problems == (42,)
    assert [e["result"] for e in events if e.get("type") == "citation_check"] == ["repair", "flagged"]


@pytest.mark.asyncio
async def test_malformed_tool_args_during_citation_repair_delivers_previous_answer():
    registry = ToolRegistry()
    model = ScriptedModel(
        ModelTurn(content="Original answer [ref:5]."),
        ModelToolArgumentsError("bad args"),
    )
    runner = AgentLoopRunner(model, registry)
    output = await runner.run(_ctx("q"))
    assert output.outcome is StepOutcome.SUCCEEDED
    assert output.content == "Original answer [ref:5]."
    assert output.citation_problems == (5,)


@pytest.mark.asyncio
async def test_citation_check_runs_before_output_contract_and_each_repairs_once():
    memory = RunMemory(RunState("u1", "q"))
    d1 = _doc("Fact one.", file_name="a.md", source="a.md")

    async def fake_search(query, history):
        return _build_evidence([d1])

    registry = ToolRegistry()
    registry.register(KnowledgeRegistryTool(memory, search=fake_search))
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    contract = OutputContract(schema)
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall("c1", "retrieve_knowledge", {"query": "q"}),)),
        # Turn 1: invalid citation AND invalid JSON -> citation check triggers first.
        ModelTurn(content="not json [ref:9]"),
        # Turn 2: valid citation, still invalid JSON -> output contract repair triggers next.
        ModelTurn(content="still not json [ref:1]"),
        # Turn 3: valid citation AND valid JSON -> passes both!
        ModelTurn(content='{"summary": "Verified [ref:1]"}'),
    )
    events = []
    runner = AgentLoopRunner(
        model, registry, output_contract=contract, citation_ledger=memory.citations,
    )
    with using_hooks(HookSet([SimpleNamespace(observe=events.append)])):
        output = await runner.run(_ctx("q"))

    assert output.outcome is StepOutcome.SUCCEEDED
    assert output.output == {"summary": "Verified [ref:1]"}
    assert [c["ref"] for c in output.citations] == [1]
    assert output.citation_problems == ()
    assert runner.format_repair_count == 2
    kinds = [(e["type"], e["result"]) for e in events if e.get("type") in ("citation_check", "output_contract")]
    assert kinds == [
        ("citation_check", "repair"),
        ("citation_check", "pass"),
        ("output_contract", "repair"),
        ("citation_check", "pass"),
        ("output_contract", "pass"),
    ]


# ---- Resume, Explore (--delegate), and Team ----

@pytest.mark.asyncio
async def test_resume_preserves_citation_numbering():
    memory = RunMemory(RunState("u1", "q"))
    d1 = _doc("Before pause.", file_name="a.md", source="a.md", start_line=1, end_line=2)
    d2 = _doc("After pause.", file_name="b.md", source="b.md", start_line=3, end_line=4)
    batches = [[d1], [d2]]

    async def fake_search(query, history):
        return _build_evidence(batches.pop(0))

    registry = ToolRegistry()
    registry.register(KnowledgeRegistryTool(memory, search=fake_search))
    store = LoopResumeStore()
    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall("c1", "retrieve_knowledge", {"query": "q1"}),)),
        ModelTurn(tool_calls=(ToolCall("c2", ASK_USER, {"question": "Need scope?"}),)),
        ModelTurn(tool_calls=(ToolCall("c3", "retrieve_knowledge", {"query": "q2"}),)),
        ModelTurn(content="Combined [ref:1][ref:2]."),
    )
    run = LoopRun(
        memory, model, registry,
        runner_factory=lambda deps: AgentLoopRunner(
            deps.model, deps.tool_registry, resume_store=store, citation_ledger=memory.citations,
        ),
        step_description="test",
    )
    r1 = await run.run()
    assert r1.status is TaskStatus.BLOCKED
    qid = memory.task_state.scratchpad.open_questions[0].question_id
    memory.task_state.resolve_question(qid, "all")
    r2 = await run.run()
    assert r2.status is TaskStatus.COMPLETED
    assert [c["ref"] for c in r2.citations] == [1, 2]
    assert r2.citation_problems == ()


@pytest.mark.asyncio
async def test_explore_member_citations_are_valid_for_lead_and_member_skips_check():
    memory = RunMemory(RunState("u1", "research"))
    d1 = _doc("Member found this.", file_name="m.md", source="docs/m.md", start_line=1, end_line=5)

    async def fake_search(query, history):
        return _build_evidence([d1])

    registry = ToolRegistry()
    registry.register(KnowledgeRegistryTool(memory, search=fake_search))
    # Lead round 0: must do 1 tool call before delegate is offered (ExploreDelegation.offer).
    # Then lead calls delegate -> member retrieves d1 and even mentions [ref:99] (member does NOT check).
    # Lead then cites [ref:1] -> valid!
    model = ScriptedModel(
        # Lead turn 1: call retrieve_knowledge or another tool? Let's call retrieve_knowledge after delegate
        # by giving lead a read_file first so member does the only retrieve_knowledge.
        ModelTurn(tool_calls=(ToolCall("l1", "read_file", {}),)),
        ModelTurn(tool_calls=(ToolCall("l2", DELEGATE, {"task": "find info", "thoroughness": "quick"}),)),
        # Member turn 1: calls retrieve_knowledge
        ModelTurn(tool_calls=(ToolCall("m1", "retrieve_knowledge", {"query": "info"}),)),
        # Member turn 2: returns report with bogus [ref:99] -> should NOT trigger repair in member!
        ModelTurn(content="Found [ref:1] and note [ref:99]."),
        # Lead turn 3: cites [ref:1]
        ModelTurn(content="Final answer based on [ref:1]."),
    )
    registry.register(SimpleNamespace(
        definition=ToolDefinition("read_file", "read", {"type": "object"}, effect="read"),
        invoke=lambda args: _async_ok("readme"),
    ))
    runner = ExploreLoopRunner(
        model, registry, LoopOptions(max_tool_rounds=8),
        options=ExploreOptions(max_delegations=1),
        citation_ledger=memory.citations,
    )
    output = await runner.run(_ctx("research"))
    assert output.content == "Final answer based on [ref:1]."
    assert [c["ref"] for c in output.citations] == [1]
    assert output.citation_problems == ()
    assert runner.format_repair_count == 0


async def _async_ok(text: str) -> ToolOutput:
    return ToolOutput(True, text)


@pytest.mark.asyncio
async def test_team_mode_does_not_check_citations():
    registry = ToolRegistry()
    model = ScriptedModel(ModelTurn(content="Team answer [ref:99]."))
    runner = DelegatingLoopRunner(model, registry, DelegationOptions())
    output = await runner.run(_ctx("team task"))
    assert output.content == "Team answer [ref:99]."
    assert output.citations == ()
    assert output.citation_problems == ()
    assert runner.format_repair_count == 0


# ---- Trace view ----

def test_trace_view_renders_citation_check_without_snippets():
    records = [
        {"type": "audit", "version": 2, "started_at": "2026-11-23T00:00:00Z"},
        {"type": "task_start", "task_id": "t1", "span_id": "s0", "parent_id": None, "t": 0.0},
        {"type": "citation_check", "result": "repair", "cited": 2, "invalid": 1, "parent_id": "s0", "t": 0.1},
        {"type": "citation_check", "result": "pass", "cited": 2, "invalid": 0, "parent_id": "s0", "t": 0.2},
        {"type": "task_end", "task_id": "t1", "status": "completed", "span_id": "s0", "parent_id": None, "t": 0.3},
    ]
    trace = build_trace(records)
    text = render_text(trace)
    assert "发现无效引用编号，要求修改一次（引用 2 处，无效 1 处）" in text
    assert "引用编号核对通过（引用 2 处）" in text


# ---- CLI report & result_record ----

def test_cli_report_prints_citations_and_problems_before_status_line():
    res = AgentRunResult(
        task_id="t1",
        status=TaskStatus.COMPLETED,
        content="Answer [ref:1][ref:9].",
        steps=(),
        execution_count=1,
        replan_count=0,
        citations=(
            {"ref": 1, "file_name": "guide.md", "source": "docs/guide.md",
             "heading_path": "Setup > Install", "start_line": 10, "end_line": 20, "snippet": "hi"},
            {"ref": 2, "file_name": "notes.md", "source": "notes.md",
             "heading_path": "", "start_line": None, "end_line": None, "snippet": "yo"},
        ),
        citation_problems=(9, 12),
    )
    lines: list[str] = []
    report(res, write=lines.append)
    joined = "\n".join(lines)
    assert "引用 [1]：guide.md > Setup > Install（第 10～20 行）" in joined
    assert "引用 [2]：notes.md" in joined
    assert "提示：答案里的引用 [9]、[12] 不在本次检索结果里（只核对了编号是否存在）" in joined
    assert lines[-1].startswith("\nStatus: completed;")


def test_result_record_omits_default_citations_and_includes_non_empty():
    plain = AgentRunResult("t1", TaskStatus.COMPLETED, "ok", (), 1, 0)
    rec_plain = result_record(plain)
    assert "citations" not in rec_plain
    assert "citation_problems" not in rec_plain

    with_cites = replace(
        plain,
        citations=({"ref": 1, "file_name": "a.md", "source": "a.md",
                    "heading_path": "", "start_line": 1, "end_line": 2, "snippet": "x"},),
        citation_problems=(5,),
    )
    rec_cites = result_record(with_cites)
    assert rec_cites["citations"][0]["ref"] == 1
    assert rec_cites["citation_problems"] == [5]


# ---- Archive round-trip ----

@pytest.mark.asyncio
async def test_archive_omits_empty_citations_and_restores_old_and_new(tmp_path):
    old_payload = {
        "task_id": "t0", "status": "completed", "content": "old",
        "steps": [], "execution_count": 1, "replan_count": 0,
    }
    restored_old = _restore_result(old_payload)
    assert restored_old.citations == ()
    assert restored_old.citation_problems == ()

    d1 = _doc("Archived fact.", file_name="arc.md", source="arc.md", start_line=1, end_line=3)

    async def fake_search(query, history):
        return _build_evidence([d1])

    model = ScriptedModel(
        ModelTurn(tool_calls=(ToolCall("c1", "retrieve_knowledge", {"query": "arc"}),)),
        ModelTurn(content="From [ref:1]."),
    )
    async with RagAgentService(
        model=model,
        mcp_servers=(),
        knowledge_search=fake_search,
        memory_directory=str(tmp_path / "archive"),
        personas_dir="",
    ) as svc:
        r1 = await svc.query_result("ask", "s1", request_id="req1")
        assert [c["ref"] for c in r1.citations] == [1]
        # Replay from archive restores citations.
        r1_replay = await svc.query_result("ask", "s1", request_id="req1")
        assert r1_replay.citations == r1.citations
        assert r1_replay.citation_problems == ()


# ---- D-033: no exception text in logs or tool error ----

@pytest.mark.asyncio
async def test_d033_knowledge_tool_hides_exception_text(monkeypatch):
    secret = "SECRET_DB_PASSWORD_XYZ"

    class BoomSearch:
        def __init__(self, *args, **kwargs):
            pass

        def dense_search_compat(self, *args, **kwargs):
            raise RuntimeError(secret)

        async def asearch(self, *args, **kwargs):
            raise RuntimeError(secret)

    monkeypatch.setattr(knowledge_tool, "RagSearchService", BoomSearch)
    monkeypatch.setattr(knowledge_tool, "get_reranker_service", lambda: None)
    monkeypatch.setattr(knowledge_tool, "get_keyword_retriever", lambda: None)

    class NoopRewrite:
        async def rewrite(self, query, history=None):
            return SimpleNamespace(rewritten_queries=[], warning="")

    logs: list[str] = []
    sink_id = loguru_logger.add(logs.append, level="WARNING")
    try:
        _, art_sync = knowledge_tool.search_knowledge("q")
        _, art_async = await knowledge_tool.asearch_knowledge("q", rewrite_service=NoopRewrite())
    finally:
        loguru_logger.remove(sink_id)

    for art in (art_sync, art_async):
        assert art["status"] == "error"
        assert "RuntimeError" in art["error"]
        assert secret not in art["error"]
    assert any("RuntimeError" in line for line in logs)
    assert all(secret not in line for line in logs)


@pytest.mark.asyncio
async def test_d033_reranker_service_hides_exception_text():
    secret = "SECRET_RERANK_TOKEN_999"

    class FailingProvider:
        async def score(self, question, documents, model):
            raise ValueError(secret)

    docs = [
        RetrievedDocument(document=_doc("a"), distance=0.2),
        RetrievedDocument(document=_doc("b"), distance=0.1),
    ]
    svc = RerankerService(provider=FailingProvider(), enabled=True)
    logs: list[str] = []
    sink_id = loguru_logger.add(logs.append, level="WARNING")
    try:
        res = await svc.rerank("q", docs)
    finally:
        loguru_logger.remove(sink_id)

    assert res.trace.rerank_used is False
    assert res.trace.rerank_warning == "rerank_failed"
    assert any("ValueError" in line for line in logs)
    assert all(secret not in line for line in logs)
