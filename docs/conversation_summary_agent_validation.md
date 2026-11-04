# Background summary reliability during Agent execution

Offline validation performed2026-10-30. This follow-up adds tests only; production components, default models and runtime configuration are unchanged.

## Executed flow

Test: tests/agent/test_summary_lifecycle_e2e.py.

1. Persist26 completed source Turns from an earlier Task, enough to trigger actual background compression outside the20-Turn recent window.
2. Start RagAgentService with the production LLMCompressor and background scheduler. Planner/model responses and the summary provider are controlled offline replacements; no external model/API is called.
3. Run a new Task through Planner → AgentRuntime → Executor/StepExecutor → ToolCallingStepRunner → real get_current_time local tool.
4. Block on an output-format question. Keep summary generation waiting on a controlled provider gate.
5. Submit a new Turn to resume the same Task and complete its report. Check execution_count3/replan_count1, exact original user text, and one clock tool invocation. Summary faults must not cause extra executions/replans.
6. Retry the same request ID and verify receipt replay without another model/tool call.
7. In the normal summary case, run another Task that discovers the committed summary reference in planner context and invokes the real read_context_artifact tool. Check the retrieved text matches the persisted summary body.
8. Compare all original source JSON bytes, check nonoverlapping summary coverage and Turn outcomes, then create a fresh service instance and replay the durable completed receipt with no model calls.

## Controlled scenarios

| Summary provider mode | Required behavior | Executed |
|---|---|---|
| Normal completion | Commit recoverable summary; Agent subsequently reads it through the tool registry |2 runs passed|
| Provider exception | Stop after2 failed attempts; no summary coverage; main Task still completes |2 runs passed|
| Truncated output | Production LLMCompressor rejects finish_reason=length; no partial summary published |2 runs passed|
| Timeout | Scheduler cancels stalled generation; finite attempts; raw sources retained |2 runs passed|
| Service close cancels active generation | Bounded close cancels provider wait; no summary is committed |2 runs passed|

Fresh execution:10 passed in7.41s, basetemp tmp/pytest-summary-agent-e2e-20261030.
Full regression:969 passed,2 skipped in26.38s, basetemp tmp/pytest-summary-agent-e2e-full-20261030.
Test source compilation passed. No commit/push or real API execution.

## What this establishes

The tested orchestration supports concurrent background generation and foreground planning/tool execution/pause/resume. Summary failures do not change the tested task outcomes or replay completed tools. Raw originals, source coverage and durable result replay remain intact in these scenarios. These are functional checks with explicit timeouts, not production latency benchmarks or a statistical reliability estimate.

## What it does not establish

- Real LLM factual accuracy, omission rate, instruction following or preservation of negations. The supplied successful summary is fixed test data.
- Real-provider quota/network behavior, token-window compatibility, actual RAG/WebSearch/MCP external-service behavior under summary load.
- Autonomous model choice to retrieve history: the controlled model explicitly requests the artifact. P2 automatic summary-body selection remains deferred.
- Live task resumption across process restart. The test replays a completed durable result in a fresh service instance; it does not restore in-flight WorkingMemory.
- Large-scale throughput, multi-process coordination or a general guarantee of exactly-once external side effects.

## Re-run

```powershell
.venv/Scripts/python.exe -m pytest tests/agent/test_summary_lifecycle_e2e.py -q --basetemp tmp/pytest-summary-e2e-UNIQUE
.venv/Scripts/python.exe -m pytest -q --basetemp tmp/pytest-summary-full-UNIQUE
```

A subsequent real-model run should use a fixed source set and check original facts/negations and retrieval answers against that source set. Keep orchestration reliability and semantic summary quality as separate results.
