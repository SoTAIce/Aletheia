"""Deterministic doc-code contract tests for Agent Team V1 (team-audit-001).

Each test pins a claim from docs/PROJECT_HANDOVER.md section 7 and
docs/agent_team_v1.md. No model, MCP, DB or file I/O.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.agent_team_runner import AgentTeamRunner, register_team_runner
from app.agent.team.contracts import ContractError
from app.agent.team.coordinator import COMMAND, REVIEW
from app.agent.team.models import TeamEnvironment, TeamOptions, TeamStore
from app.agent.team.session import SessionError, TeamSession


def test_documented_default_limits() -> None:
    options = TeamOptions()
    assert (options.max_members, options.max_actions, options.max_model_calls,
            options.max_tool_calls, options.max_revisions, options.timeout_seconds) == (
        4, 32, 48, 32, 12, 180)
    assert options.max_estimated_tokens == 500_000


def test_default_tools_are_read_only() -> None:
    assert TeamOptions().allowed_tools == frozenset({
        "get_current_time", "read_file", "list_directory", "retrieve_knowledge", "search_web"})


def test_default_host_contract_requires_nonempty_answer() -> None:
    contract = TeamOptions().output_contract
    assert contract.serialize({"answer": "ok"}) == '{"answer": "ok"}'
    for payload in ({"answer": "  "}, {"answer": "ok", "extra": "x"}, {}):
        with pytest.raises(ContractError):
            contract.validate(payload)


def test_review_feedback_is_capped_at_2000_chars() -> None:
    REVIEW.validate({"candidate_version": 1, "decision": "approve", "feedback": "x" * 2000})
    with pytest.raises(ContractError):
        REVIEW.validate({"candidate_version": 1, "decision": "approve", "feedback": "x" * 2001})
    with pytest.raises(ContractError):
        REVIEW.validate({"candidate_version": 1, "decision": "maybe", "feedback": ""})


def test_lead_command_rejects_unknown_action() -> None:
    base = {"member_id": "", "target_id": "", "instruction": "", "tools": "", "expected_version": 0}
    COMMAND.validate({**base, "action": "create_worker"})
    with pytest.raises(ContractError):
        COMMAND.validate({**base, "action": "spawn_team"})


def test_member_revision_limit_allows_twelve_revisions() -> None:
    class _Model:
        async def generate(self, messages, tools):  # pragma: no cover - never called
            raise AssertionError

    session = TeamSession("worker", _Model(), lambda: None, asyncio.Queue(), "goal",
                          max_revisions=TeamOptions().max_revisions)
    for _ in range(12):
        session.revise(session.version, "rework")
    assert session.version == 13
    with pytest.raises(SessionError):
        session.revise(session.version, "one too many")


def test_production_runner_requires_ownership_guard() -> None:
    environment = TeamEnvironment("u", lambda *a: None, lambda m: {}, capture_guard=None)
    with pytest.raises(ValueError):
        AgentTeamRunner(object(), TeamOptions(), TeamStore(), environment)


def test_registration_intersects_allowlist_with_available_tools() -> None:
    captured = {}

    class _Registry:
        def register(self, definition, factory):
            captured["definition"], captured["factory"] = definition, factory

    environment = TeamEnvironment("u", lambda *a: None, lambda m: {}, capture_guard=lambda c: (lambda: True))
    register_team_runner(_Registry(), options=TeamOptions(), store=TeamStore(), environment=environment)
    assert captured["definition"].runner_id == "agent_team"
    available = [SimpleNamespace(name="read_file"), SimpleNamespace(name="write_file")]
    deps = SimpleNamespace(model=object(), context_manager=None,
                           tool_registry=SimpleNamespace(list_definitions=lambda: available))
    runner = captured["factory"](deps)
    assert runner.options.allowed_tools == frozenset({"read_file"})
