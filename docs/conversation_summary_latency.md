# Offline foreground latency measurement

Measured2026-10-30 on the current local Windows workspace. No production changes, real model requests or external-service calls.

## Method

Runner:tmp/measure_summary_latency.py. Raw samples/settings:tmp/summary-latency-20261030-410def73/report.json.

Each case starts with the same40 persisted completed historical Turns (~3000 characters of padding per user message). Every measured request follows the real service/planner/runtime/tool loop, invokes the actual local clock tool and persists its lifecycle outcome. Planning/tool-calling model responses are immediate controlled replacements. Background summaries use the production LLMCompressor with a controlled provider replacement.

Three modes, repeated in rotating order over3 rounds: compression disabled, summary provider held waiting until all foreground requests finish, summary provider delayed80ms then allowed to commit during foreground requests. Each case has one separately reported first request and24 subsequent requests. Warm aggregate sample count72 per mode;225 total requests including the9 first requests. All asserted COMPLETED, one successful tool call and zero replans.

## Results

| Mode | Warm median | Warm P95 | Warm maximum |
|---|---:|---:|---:|
| Compression disabled |143.653ms|172.153ms|196.015ms|
| Background provider held waiting |175.077ms|205.186ms|206.997ms|
| Background generation/commit |174.817ms|207.222ms|223.452ms|

Versus disabled, background generation/commit adds31.164ms to the pooled median and35.069ms to the pooled P95. These are differences of aggregate statistics, not paired-request percentile measurements.

First requests range171.9–180.702ms with compression disabled and238.672–274.395ms with background modes. They include session initialization/recovery and are excluded from the warm table. They are not an operating-system cold-cache benchmark.

The held-provider case completes all foreground requests before the provider gate is released, demonstrating independence from that long model wait. Each committing case published3 summaries during foreground measurement. The injected80ms wait is test stimulus, not a measured real-provider latency. Post-measurement shutdown latency is recorded separately in raw rows.

## Interpretation and limits

The tested local overhead is about0.03s median /0.035s P95 extra for archive-backed background compression. This is a reasonable provisional cost for the current Agent workflow, not evidence that real user-visible latency meets a specified SLA. The existing archive reads, history projection and small commit markers still cost local CPU/disk time.

End-to-end real latency also includes every planning/execution model request, tool service time and any repairs/replans. No real-model time-to-first-token, streaming responsiveness, local Ollama inference contention or external RAG/MCP/WebSearch load was measured. In particular, simultaneous main-model and summary-model inference on the same GPU/CPU may behave differently from these controlled providers.

Sample sizes are modest; this is a sequential single-session measurement, not a concurrency stress test or statistical production reliability claim. The workload's history length/text distribution and machine cache/storage conditions affect the numbers.

Re-run without changing model configuration:

```powershell
.venv/Scripts/python.exe tmp/measure_summary_latency.py
```
