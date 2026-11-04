# Conversation I/O Harness Results ? 2026-10-25

## Delivered and scope

Installed walkinglabs/learn-harness-engineering skills/harness-creator into the user's Codex skill directory using skill-installer. It is discoverable on the next turn; this turn read and applied its installed SKILL.md directly.

Root harness: AGENTS.md (routing/invariants), feature_list.json (feature state), progress.md (evidence), session-handoff.md (restart), init.ps1 (native Windows verification) and init.sh (Git Bash alternative). No automatic commits/pushes. Structural scoring is separate from runtime performance.

Production changes are only app/services/conversation_archive.py and app/models/conversation_memory.py. Existing user changes to ContextManager/RagAgentService and tests were preserved. No new dependencies, models, API calls or warm-body-cache implementation.

## Stage 1

Derived, per-instance filename metadata stores lstat fingerprint, original turn ID and estimated source tokens. Audit all filenames and file attributes on each operation; decode only new/changed originals. Duplicate writes re-read the target and compare the complete canonical record. Publication still uses temp file, fsync and atomic replace; an exception never marks an unwritten original as published.

RED tests showed warm append decoded 40 prior bodies and a middle duplicate decoded 21. GREEN stage-1 scoped: 52 passed, 1 skipped. Stage-1 full: 7 failed, 902 passed, 2 skipped; all seven failures reproduced using original source snapshots (see baseline below).

## Stage 2

Bounded load validates metadata, then reads only recent K bodies. During cold/changed-file audit it temporarily reuses only the requested tail's decoded objects, avoiding a second decode; no body cache survives the call. Unbounded load_turns() remains a full-body verification. Pending summary selection sums validated token metadata and reads at most the chosen batch's bodies.

ConversationMemory.add uses one worker call holding the session RLock across public save/reload. It does not append caller objects into cache or skip revision checks. Both operations still audit metadata; no exception-sensitive skip flag or stale shared snapshot was introduced. With a compressor, the later pending-source operation remains a separate bounded worker.

Additional RED/GREEN gates cover <=K warm body reads, <=8 pending reads, single worker, clear/cancellation, durable-save/reload failure retry and cold tail decoding only once. Independent review found one Important mutable-input race: metadata originally read the caller's turn after publication. Fixed using the same durable record snapshot for ID, filename and tokens; regression first failed with duplicate IDs then passed. Final independent review found no remaining Critical/Important.

## Tests and limitations

- Final focused (archive, model, summaries, new I/O, artifacts): **66 passed, 1 skipped**.
- Native init.ps1: run using the process-only policy command below because the current Windows default disallows script execution; it checks syntax and four focused suites. No system execution-policy setting is changed.
- Final full suite: **7 failed, 909 passed, 2 skipped**, 13.45s (tmp/pytest-conv-final-full-20261025).
- Baseline replay loading the two untouched source snapshots into pytest, ignoring new I/O tests: **7 failed, 895 passed, 2 skipped**, 13.73s. The same seven tests fail at app/services/rag_agent_service.py:75 because math is not imported in pre-existing user edits. This task did not modify that module. Full suite is NOT green.
- Windows symlink privilege coverage is skipped where unavailable; deep-path/reparse fallback tests ran. The second full-suite skip is pre-existing.
- No new regression relative to the source baseline; no live model or user data benchmarks.

## Runtime benchmark

Identical offline workload, Windows local filesystem, 1024-character originals, recent window 20, 3 samples/case, median milliseconds. Seed setup is outside measurement; atomic writes/fsync remain enabled. Sizes 100 and 500; append samples grow history by one per repetition, hence baseline median counts 101/501. All samples, counts, environment and generated synthetic fixture locations are in tmp/conversation-bench-*-serial.json.

These final runs were serial, not concurrent with full pytest. Benchmark imports trusted local source snapshots for before/stage-1 and current code for stage-2. Earlier exploratory JSON files are retained but are not the final comparison.

| History | Operation | Before ms | Stage 1 ms | Stage 2 ms | JSON body reads before / stage1 / stage2 |
| --- | --- | ---: | ---: | ---: | ---: |
| 100 | save | 34.109 | 3.645 | 4.157 | 101 / 0 / 0 |
| 100 | add | 74.423 | 39.114 | 12.889 | 203 / 103 / 20 |
| 100 | add_compressor | 101.672 | 70.170 | 17.460 | 285 / 186 / 28 |
| 100 | warm_load | 31.780 | 33.177 | 8.775 | 100 / 100 / 20 |
| 100 | cold_load | 33.364 | 32.911 | 39.359 | 100 / 100 / 100 |
| 100 | full_load | 32.411 | 32.687 | 32.412 | 100 / 100 / 100 |
| 500 | save | 160.656 | 13.452 | 10.888 | 501 / 0 / 0 |
| 500 | add | 330.371 | 180.000 | 29.994 | 1003 / 503 / 20 |
| 500 | add_compressor | 515.545 | 361.576 | 40.056 | 1485 / 986 / 28 |
| 500 | warm_load | 164.109 | 157.990 | 15.994 | 500 / 500 / 20 |
| 500 | cold_load | 165.367 | 170.161 | 198.607 | 500 / 500 / 500 |
| 500 | full_load | 171.525 | 166.454 | 165.522 | 500 / 500 / 500 |

For 500 originals, save is about 14.8x faster, ordinary add 11.0x, add with a non-triggering compressor 12.9x, warm recent load 10.3x. These are local descriptive results, not portable speed guarantees. Cold recent load regressed about 20% (165.367 -> 198.607 ms), while still decoding exactly 500 originals: it additionally builds the metadata catalog. Full-body load is essentially unchanged within timing noise.

Remaining costs: directory enumeration/sorting and lstat audit remain history-dependent (sorting O(H log H), metadata O(H)); save does one audit, add two, compressor path three. Metadata memory is O(H); changed files/cold restart still require body validation. No O(1) append claim. Hostile edits that preserve all fingerprint fields are outside the existing application-owned-directory threat model; explicit full load still re-reads every body.

Reproduction:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
.venv/Scripts/python.exe scripts/benchmark_conversation_memory.py --source-snapshot tmp/conversation-harness-baseline-20261025 --label before --output tmp/conversation-bench-before-replay.json
.venv/Scripts/python.exe scripts/benchmark_conversation_memory.py --source-snapshot tmp/conversation-harness-stage1-20261025 --label stage1 --output tmp/conversation-bench-stage1-replay.json
.venv/Scripts/python.exe scripts/benchmark_conversation_memory.py --label after --output tmp/conversation-bench-after-replay.json
```

## Three-tier decision

See docs/conversation_memory_tiers.md. Keep hot recent cache + disposable metadata/index + cold originals now. A new bounded warm summary/excerpt cache is technically feasible, but only warranted if repeated-history-read traces meet hit-rate/latency/memory gates. It would not fix the remaining metadata audits. No tiered body cache was implemented.

## Harness validation
Installed validator100/100, bundled scaffold self-checkPASS, structural eval coverage10/10. See tmp/conversation-harness-structural.json and HTML reports. This checks artifact structure, not the Agent semantic behavior or application runtime performance. Native verification entry59passed1skip; one .pytest_cache write-permission warning is not a test failure.
