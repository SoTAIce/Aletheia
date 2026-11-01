# Multi-Runner real model and MCP validation

Validated2026-10-31 with configured qwen3.8-flash, real local clock tool and real filesystem MCP subprocesses. No model replies, MCP transport or tool outputs were mocked. All data are synthetic fixtures in unique application tmp directories; no private project documents were sent.

## Final independent commit-tree results

Source snapshot:tmp/runner-commit-tree-021a6bea, reconstructed from HEAD41ce1fc plus only Runner changes. Raw report:tmp/live-multi-runner-3372c1ae/report.json;33 real model requests. Assertions passed for all3 scenarios.

| Scenario | Actual work | Result |
|---|---|---|
| Multiple tools | Read metadata.json/prices.csv through MCP, compute total106, obtain UTC clock, write report.json and read it back; verify random reference/currency/total/timestamp |PASS,31.48s|
| Pause/resume | Complete evidence-file read, block for output format, close first MCP connection, resolve question, reopen MCP and rebind same Runtime; write/read JSON output using saved evidence |PASS,37.21s|
| Replan | Real read_file(primary.txt) fails because file is absent; Runtime replans, reads real backup.txt, writes recovery.json and reads it back |PASS,17.80s|

The pause check asserts the completed evidence file is read exactly once across both runs, task identity is retained, the dispatcher is replaced, and the resumed phase uses the new MCP connection. Both closed adapters reject further calls. All route records select the existing tool_calling Runner and match actual execution counts.

Replan coverage includes an explicit validation-only fail-fast hook: after the real MCP missing-file reply, the harness promotes that failed reply to StepToolError. This forces the Runtime Replan branch instead of letting the model recover inside the same Runner round. It does not fabricate a failed reply, alternate successful reply, plan or model answer; the planner and recovery tools remain real. No such hook is installed in production.

## Retained working-tree trials

- tmp/live-multi-runner-e5d8b6b8/report.json:pause/resume and Replan passed. The initial broad multi-tool goal failed StepFormatError after writing a correct file but before readback. It is retained as a real failure, not counted as completion.
- tmp/live-multi-runner-08006231/report.json:multi-tool goal clarified into three independent read/compute, write and readback steps, with unchanged Runner limits/validation; passed36.58s and12 model requests.
- Final independent snapshot then ran all3 with the clarified reproducible goal and passed. This is three functional scenarios, not a statistical production success-rate estimate. Model formatting/planning can still fail.

## Commit isolation and regression

HEAD precedes several uncommitted Turn/summary/long-term-memory changes. The isolated commit intentionally stages only Runner-related changes against that HEAD:
- New Runner registry, synchronous routing policy, independent trusted requirements and dispatcher.
- Runtime assembly/rebind and StepExecutor stale/cancellation guards.
- StepRoutingError and service dependency-injection options.
- Related tests, this report and the opt-in live script.

New Turn persistence, service cross-Turn convenience API, summary scheduling/context changes and user-authored docstrings remain in the working tree, not this commit. Direct Runtime pause/resume/rebind is supported by the isolated commit. The HEAD service already propagates stale errors without newer Turn cleanup; the working tree additionally retains its dedicated failed-Turn bookkeeping.

Actual isolated snapshot tests:539 passed,1 skipped, including41 new Runner tests. The additional current-workspace test of the uncommitted Turn-specific service cleanup remains outside this commit. The complete working tree passed1024 tests,2 skipped. Independent review checked the isolated tree and repeated its41 new tests.

## Reproduce

With the existing model configuration and project dependencies:

```powershell
.venv/Scripts/python.exe scripts/validate_multi_runner_live.py --live
# Or a single scenario:
.venv/Scripts/python.exe scripts/validate_multi_runner_live.py --live --case pause
```

The --live flag is mandatory. Calls/timeouts/execution/replan limits are finite; results, model/tool traces and generated files are retained in a fresh tmp directory. The script exits nonzero on failed acceptance checks. No real model default/configuration was changed. No push is part of this work.
