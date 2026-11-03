# ADR-005: Outage monitor is a single-instance service
**Status:** accepted, with known limit
**Context:** Poisson/EWMA/CUSUM detectors keep per-tower counters in memory.
**Decision:** `monitor` is pinned to `max_instance_count = 1`. Pub/Sub events are keyed by tower so ordering holds. On cold start the detector is rebuilt from the recent window of `interactions_fact` in BigQuery (best-effort: if the warehouse is unreachable it logs a warning and starts empty, giving possible false negatives for up to one detection window). `min_instance_count = 1` keeps it warm.
**Consequences:** One instance caps throughput (~thousands of events/s – ample for incident signals, not for full-volume fan-in) and restart loses state. Scale-out path: shard by `hash(tower_id) % N` with N Pub/Sub filtered subscriptions, or move counters to Memorystore. Not built.
