# Team PoC output ownership and bounded feedback implementation plan

Use TDD and verification-before-completion. Work in place; preserve all dirty user sources. No commits, dependencies, production Runner integration or changes to Runtime/ToolCallingStepRunner/Multi-Runner V1.

1. RED/GREEN immutable host-owned exact JSON contracts (small explicit scalar-field subset, not a new JSON Schema engine), Member submit_result protocol, canonical serialization and bounded submit-only repair. Plain text is not accepted as final output for contracted Sessions; Lead prose cannot replace the contract.
2. RED/GREEN reviewable submissions keep the same owning task/MCP/history alive. Host accept vs revision controls are version checked. Preserve legacy uncontracted PoC behavior. Revisions cannot reset model/tool/repair budgets; stale outputs remain rejected.
3. RED/GREEN new small bounded ReviewTrial, leaving the original single-cycle CoordinationTrial as historical experiment. Persistent independent Lead and Worker; fresh reviewer per candidate, current-version reviews, host semantic validation, multiple feedback/review/rework cycles, cancellation and fail-closed limits. No general task board/persistence.
4. Deterministic trials: hostile Lead format instructions, invalid submissions, no business-tool replay on format repair, two Worker coordination requests, rejected draft -> same Worker revision -> new review -> acceptance, stale review rejection, cancellation and cycle exhaustion. Preserve existing22 tests and real MCP owner-task behavior.
5. Independent review, full regression, protected-source hash comparison. Add opt-in synthetic real-model+MCP validation with immutable expected nonce/amount/plan checks and version-bound review. Retain all failures; report model-autonomous rework separately from explicitly seeded negative-control drafts. Never weaken host assertions.
6. Record actual deterministic/live evidence and remaining assumptions in docs/team_contract_review_poc.md and harness ledgers. Bounded API calls only; no private data or key logging.

Completed evidence:39 scoped+independent tests; full1063pass2skip. Live strict feedback+seeded rework PASS28calls in06ad674e; final-source rework rerunPASS15calls in1275968c. See docs/team_contract_review_poc.md for source timing, negative-control labels and limitations. No production integration or commits.
