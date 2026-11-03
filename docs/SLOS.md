# Service-level objectives (proposed)

Targets are proposals sized from the measured single-thread latency (end-to-end p95 ≈ 169 ms per conversation in-process, ~10 conv/s/thread) plus Pub/Sub and Cloud Run overheads. They have **not** been validated under production load; run a load test (`scripts/demo.py` at volume or k6 against the gateway) before committing them.

| SLI | SLO | Alert (Terraform `monitoring.tf`) |
|---|---|---|
| Gateway availability (non-5xx / all) | 99.9 % / 30 d | 5xx ratio > 2 % |
| Gateway p95 latency (accept + publish) | < 500 ms | p95 > 2 s |
| Ingest → enriched event lag | p95 < 30 s | oldest unacked > 600 s |
| Ingest → analysis stored | p95 < 2 min (routine), < 10 min (swarm with LLM) | pipeline lag alert |
| Dead letters | 0 sustained | DLQ backlog > 0 for 10 min |
| Outage detection delay | median ≤ 10 min, max ≤ 30 min (eval: 9.5 / 16) | eval gate, not a runtime alert |
| PII leakage into redacted zone | 0 | cloud re-scan rejects; privacy incident process |
| Unauthorised actions executed | 0 | policy test gate; kill-switch alert |

Error budget policy: if the monthly budget is exhausted, freeze non-security releases until it recovers.
