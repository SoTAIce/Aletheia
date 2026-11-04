# Conversation I/O Optimization Plan

Goal: reduce repeated history body reads while preserving original files, public interfaces and recovery semantics. User authorizes implementation now, no commits/pushes. Use installed harness-creator's five subsystems; progress lives in root feature_list.json/progress.md/session-handoff.md.

## Baseline and scope
- Read save_turn, load_turns, pending_summary_turns, ConversationMemory.add/restore and focused tests before editing.
- Existing save validates every JSON body, add reloads every JSON body again; pending summary selection reads older bodies a third time.
- Existing per-session RLock serializes same-process archive instances; asyncio lock/revision guard protects memory. Keep both.
- Snapshot two owned source files in tmp/conversation-harness-baseline-20261025. Do not touch unrelated user edits.
- No original-format migration or persistent database/manifest. Derived metadata is disposable and rebuildable.

## Stage 1 / conv-001 — validated metadata reuse
- [x] Write RED tests: warmed append does not decode unchanged history; duplicate still validates target contents; in-place corrupt modification, foreign record, duplicate sequence/ID and cross-instance write are detected; failed fsync/replace followed by retry retains correct ordering.
- [x] Maintain per-instance derived metadata indexed by filename, with full lstat fingerprint (identity, size, nanosecond mtime/ctime, mode/reparse attributes), turn ID and estimated source tokens. Retain no historical bodies.
- [x] Each audit enumerates and validates filenames/sequences, stats records, parses new/changed records, checks duplicate IDs, and publishes the metadata snapshot only after complete validation.
- [x] save_turn uses that snapshot to locate duplicate/next sequence; re-read duplicate body to compare the incoming canonical record. Publish original atomically as before. Failed publication must not mark an unwritten record as cached.
- [x] Validate same-process multiple instances and restart. Keep load_turns full-body behavior in this stage to isolate the effect.
- [x] Run scoped tests, full regression and baseline-identical benchmark. Record timings and JSON read counts; mark done only with evidence.

Important tradeoff: this removes O(H) JSON/body parsing per warm save, not O(H) directory/stat checks. Full cold audit is still O(H). Fingerprints detect ordinary file changes, not adversarial edits restoring all metadata; the pre-existing application-owned-directory assumption remains. Do not replace audits with directory-mtime-only trust.

## Stage 2 / conv-002 — bounded add path
- [x] Write RED tests: warm load_turns(K) decodes at most K bodies; pending scan loads only chosen bounded batch; add uses one worker for atomic save/recent reload, no change to public save_turn/load_turns signatures or returns.
- [x] Use validated metadata for limit selection and pending token totals. Full load_turns(None) remains an explicit full-body audit/read; bounded load validates changed cold records before returning recent ones.
- [x] Private archive save+recent-load helper holds RLock across both public operations. Scoped audit reuse may avoid duplicate metadata audits only while that lock is held; discard the shortcut on all success/error paths. Never retain per-thread global bypass state.
- [x] ConversationMemory.add awaits the combined worker and only mutates recent cache after success/revision check. Preserve cancellation-during-worker, clear, pop/remove reconstruction, duplicate retry and peer-instance visibility.
- [x] Retain complete-record ownership: retrieved/mutated ConversationTurn objects never mutate originals or metadata cache.
- [x] Run scoped/full tests and same benchmark again. Archive-disabled and no-compressor paths must remain compatible.

## Benchmark and acceptance
- Synthetic session sizes 100 and 500; fixed timestamps/IDs, fixed 1024-character text, 3 append repetitions, max_turns=20. No real APIs/models.
- Separate warmed save, warmed add with/without compressor, cold bounded load, and full-history verification.
- Report median and samples in milliseconds, _read JSON-body decode count and _paths audit count. Seed data with direct immutable files outside measured timings to avoid O(H²) setup skew.
- Hard deterministic gates: unchanged warm save decodes 0 historical bodies; duplicate validates exactly its target; warmed bounded load <=20; summary reads <=batch_limit; add performs no full-history body reload. Cold validation/error detection must not be skipped to hit the target.
- Timing is descriptive on the same local filesystem with fsync kept enabled. No flaky hard timing threshold. Explicitly report remaining O(H) metadata work.
- Commands: see AGENTS.md and scripts/benchmark_conversation_memory.py. Use unique workspace pytest basetemp paths.

## Stage 3 / conv-003 — design only
- [x] Document existing cache/index/archive vs proposed hot recent turns, warm bounded decoded summaries/original snippets, cold immutable originals.
- [x] Specify byte/token bounds, session keys, deep-copy semantics, invalidation, cancellation and crash reconstruction; evaluate complexity and expected benefit.
- [x] Recommend whether warm caching is justified by measured misses. No warm body cache, new tiers, model calls, queues or background compression implementation.

## Completion
- [x] Run installed skill structural validator; clearly separate structural harness score from runtime benchmark.
- [x] Write report with stage tests, before/after measurements, limitations and actual modified files.
- [x] Update feature state/progress/handoff. Do not commit or push.

## Completion evidence
See docs/conversation_memory_io_results.md and progress.md. Scoped gates pass; seven existing full-suite failures reproduced with baseline source. Cold-start regression reported. No commit/push.
