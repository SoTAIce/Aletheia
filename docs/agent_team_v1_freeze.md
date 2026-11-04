# Agent Team V1 local freeze

Date:2026-11-03. Base commit:adea10e319f2909ea59c4e659ed9a8d820daff09. User authorized one local independent commit, no push.

The freeze contains AgentTeamRunner, independent Lead, deterministic sequential Coordinator, isolated resumable Sessions, immutable output contracts/version-bound review, shared budgets/permissions, safe diagnostics and Reviewer submission-only repair. Normal ToolCallingStepRunner and routing implementation stay at the base commit. The service's already-tested Turn/archive/summary lifecycle is included as a required dependency for cross-Turn Team pause/resume; no lifecycle redesign was performed during freezing.

Unfinished long-term recall injection and long-term ContextManager projection are deliberately excluded from the committed service/context blobs. User's Planner, StepExecutor and ToolCallingStepRunner docstring edits remain unstaged. Existing tests are preserved as the complete regression gate. No secrets, .env, runtime/cache/model outputs or dependencies are added.

Verification before committing:
- Fresh working checkout:1093 passed,2 skipped,23.92s (`tmp/pytest-team-freeze-workspace-1103`).
- Independent dependency-closed candidate built from HEAD:1093 passed,2 skipped,24.87s (`tmp/pytest-team-freeze-candidate-1103`).
- Candidate source roots: `tmp/team-v1-freeze-candidate-1103`, provenance/selection/preexisting hashes: `tmp/team-v1-freeze-manifest-1103.json` (local artifacts, excluded from Git).

The exact candidate blobs are staged, not a broad git add of the dirty checkout. The independent candidate has the same full regression count while leaving unfinished/unrelated work out. Real-task evaluation after this freeze must be recorded separately; a passed suite does not establish general Team accuracy or latency. Previous real runs and their failures are retained in the usage report.
