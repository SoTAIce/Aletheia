# ConversationMemory: three-tier cache proposal (design only)

## Current state and boundaries

ConversationMemory owns recent turn objects (default 20), a bounded pending source batch and summary metadata. ConversationArchive owns immutable originals, summary/artifact bodies and source references. ContextManager selects a model-visible projection; WorkingMemory owns task truth. Long-term memory is a separate cross-session fact system. None of these responsibilities should move into a new cache controller.

This optimization adds only disposable file metadata (fingerprint, ID, token count), not a persistent decoded-body warm tier. A temporary recent-window dictionary during cold audit lives for one call. Original JSON files remain authoritative and restartable without the metadata cache.

## Proposed tiers

| Tier | Contents | Owner | Proposed bounds | Miss/recovery |
| --- | --- | --- | --- | --- |
| Hot | Recent complete turns | ConversationMemory | Existing max_turns; consider an additional byte cap later, preserving whole turns | Restore recent originals from archive |
| Warm | Frequently read summary bodies and older original excerpts | ConversationArchive internals, not a duplicate Memory service | Proposed 32 entries AND 256 KiB UTF-8 payload per archive instance; reject an entry larger than cap | Read and validate the original file/ref, then bounded LRU admission |
| Cold | All immutable original turns, summary bodies and tool artifacts | ConversationArchive | Disk/application retention policy, never LRU eviction of originals | Existing atomic files, session-bound reads and summary commit markers |

The numbers are a future experiment configuration, not implemented settings. UTF-8 payload is a simple bound, not exact Python heap usage; the entry cap limits object overhead. Hot max_turns is not a byte bound: one huge turn may still consume significant memory. Summary index growth is a separate metadata scalability concern.

## Invariants and implementation choices if pursued

- Always write durable originals before hot/warm admission. Cache miss never means a missing authoritative record.
- Keys include archive root/session digest plus turn/ref identity, version where applicable, and file fingerprint. Never key by user text or title alone.
- Validate session, path and fingerprint before warm use. Invalidate on changed/deleted records; summary drafts remain invisible until committed. Root/session changes never reuse entries.
- Keep warm cached payload immutable serialized text, or deep-copy decoded dataclasses on return. Caller mutation must not alter cache identity or durable source tokens.
- Admit only completed successful reads. An exception/cancellation may leave a durable original but must not publish a partial cache value. clear() retains its current view-only semantics; it is not disk erasure.
- Preserve one session lock for metadata/cache mutation. Do not hold an extra lock across model summarization. No background threads/tasks beyond existing worker I/O are required.
- Evict only warm payloads; retain source refs and cold originals. Reconstruct hot/index from disk after restart; warm cache need not persist.
- Avoid a separate "warm summary of summary" representation: body caching changes I/O, not semantics. Stage summaries still originate from originals.

## Benefits and costs

Potential benefits are repeated summary/raw-artifact reads and lower tail latency for reused historical facts. Costs include cache admission, accounting, invalidation, mutation isolation, duplicated bodies across instances, and more cancellation/race tests. It does not fix O(H) metadata audits, full keyword scans, retrieval quality, model token budget or summary accuracy.

The measured optimized append path now decodes only the recent window (20) plus a selected summary batch (up to 8), while retaining O(H) filenames/stats. A warm body cache therefore attacks a smaller remaining cost than the original repeated full-history JSON decoding. It must not be justified simply by calling the current layout "three tiers".

## Decision and experiment gate

**Do not implement the warm tier now.** Keep hot recent turns + metadata/index + cold originals. First instrument actual repeated read IDs, payload bytes, miss rate and P50/P95 I/O latency using an offline representative session trace.

A bounded LRU prototype is justified only if repeat-read traces demonstrate useful reuse and meet predeclared gates: >=50% warm hit rate, >=20% P95 read-latency reduction, no meaningful append/cold-start regression (>10% median on repeated paired runs), and all isolation/corruption/cancel tests pass within the 32-entry/256-KiB caps. These are proposed engineering gates, not measured claims. Stop if corpus access is mostly one-pass or metadata audit dominates.

If metadata O(H) becomes the bottleneck, separately assess a rebuildable persistent catalog/journal with atomic publication and corruption recovery. If summary-index heap becomes the bottleneck, separately assess bounded/indexed discovery. Neither is solved by adding a warm body cache and neither is implemented in this task.
