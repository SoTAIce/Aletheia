# Runner live end-to-end validation and isolated commit

User explicitly authorizes real-task testing and one local Git commit; no push. This supersedes older no-API/no-commit harness instructions for this follow-up only.

- [x] Inspect HEAD/working-tree/index; preserve every unrelated uncommitted change. Index started empty.
- [x] Add bounded opt-in live harness using configured real model, real filesystem MCP subprocess and local clock. Synthetic files in a unique tmp directory only; no private transcripts/keys/default changes.
- [x] Cover multi-tool report creation/readback; completed evidence then blocked question, close/reopen MCP and Runtime.rebind; real missing-file failure promoted by harness fail-fast boundary for deterministic Runtime Replan. Report injection honestly, never fake model responses.
- [x] Capture model/tool/route traces, counters, generated files, MCP closed-client rejection and acceptance checks. Keep failed artifacts.
- [x] Prepare a self-contained staged snapshot from HEAD plus Runner-only changes. Prior uncommitted Turn/summary/longterm changes were not swept into the commit. Test snapshot separately; working tree untouched.
- [x] Run current full regression, snapshot regression and real snapshot scenarios; inspect exact staged diff, whitespace/secrets scope.
- [x] Commit only approved Runner files/tests/live harness/report with message refactor(agent): introduce multi-runner routing infrastructure. Commit adea10e; no push.
- [x] Verify commit/index/remaining dirty work; update evidence/handoff and report hash/results/limitations.

Ruling: HEAD predates uncommitted Turn lifecycle and summary work. Stage Runtime/service Runner integration against HEAD without unrelated lifecycle code; retained working-tree code still contains all current features. Use an isolated reconstruction to validate the actual commit tree, rather than assuming working-tree tests validate partial staging.

Evidence:independent tree tmp/runner-commit-tree-021a6bea, final539passed/1skip6.46s; independent review41 Runner tests. Full workspace1024passed/2skip21.74s. Actual tree live tmp/live-multi-runner-3372c1ae/report.json all3pass/33 real model calls. Pilot e5d8b6b8 retained multi-task StepFormatError; clarified3step goal passed08006231, then final tree all3passed. No production logic/default changes for model-format pilot failure.
Before commit, exact13 staged files matched the tested snapshot, selected working-file hashes unchanged, index initiallyempty and no config/data staged. One local commit adea10e created; earlier uncommitted features remain local.
