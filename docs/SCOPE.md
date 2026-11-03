# Scope: specification → delivered system

Legend: ✅ built and tested here · 🔶 built, but only exercised against simulators/emulators · 🔁 deliberately replaced (ADR) · ⛔ out of scope (stated honestly)

## Original specification (v1) vs v2

| Spec area | v2 status | Notes |
|---|---|---|
| Phase 1 ingest (SIP/RTP, Kafka, OpenStack) | ⛔ / 🔁 | Input is **transcripts** (chat JSON, diarised call transcripts). Ingest is an authenticated REST gateway + Pub/Sub. Audio capture, Kafka and OpenStack are the telco's edge and are not re-implemented. |
| Phase 2 diarisation / ASR | ⛔ | No audio models. ASR errors are *simulated* (`data/noise.py`) and robustness is measured against them (`ASR noise` eval); real-WER validation needs real ASR output. |
| Phase 2 PII/PCI redaction | ✅ | Detectors + HMAC tokens + vault + dual pass + signed attestation + cloud re-scan + GDPR erasure. Presidio/spaCy NER not used (see KNOWN_LIMITS: names rely on cue + known-name lists). |
| Phase 3 semantic understanding | ✅ | Issue/intent, entities (incl. competitors, towers), sentiment arc, sarcasm cue, code-switch handling, friction, churn cues. Classical NLP, no transformer fine-tuning (ADR-003). |
| Phase 4 agentic root cause (RAG + BSS/OSS) | ✅ | Hybrid BM25+LSA retrieval over the tau2 policy/manual, case-based voting, authoritative BSS/OSS verification, closed-set LLM tiebreak. |
| Phase 5 actions (CRM, ticket, credit, retention) | ✅ / 🔶 | Broker + policy + HITL + kill switch + deferral are tested end-to-end against the simulator. Salesforce/ServiceNow/billing connectors are **scaffolds not exercised against real systems**. |
| Phase 6 analytics / OLAP | ✅ / 🔶 | BigQuery fact tables + insight endpoints. SQL runs on SQLite in tests; BigQuery dialect helpers are unit-tested, not run on BigQuery. Looker/Dataflow not built. |
| Phase 7 drift, QA, feedback | ✅ | PSI, JSD, KS, calibrated MMD, OOV; trajectory QA; HITL sampling + verdict store. Retraining is manual-trigger (`make train`), automated promotion is not built. |
| 8.1 business KPIs | 🔶 | `value/model.py` computes *expected* value from stated assumptions. **No uplift is measured** – that needs an A/B test on live traffic. |
| 8.2 agentic metrics (GCR, TSA, TER, hallucination) | ✅ | Computed in the agents eval (action P/R, trajectory efficiency, faithfulness, citation validity, hostile-LLM containment). |
| 8.3 RAG metrics | 🔶 | Retrieval R@k/MRR with *weak supervision* (chunk relevant if it mentions a step the gold solution performs); faithfulness is exact (claim→evidence resolution). |
| 8.4 NLP metrics | ✅ | Issue/root-cause F1, ECE, competitor extraction, sentiment trend, sarcasm; WER is only for the simulator. BERTScore/ROUGE: ⛔ (no reference summaries exist). |
| 8.5 classical ML | ✅ | Churn AUC/PR-AUC/Brier/lift, resolution AUC (final and early-prefix), calibration. |
| 8.6 infra metrics | ✅ | Prometheus `/metrics` per service, latency suite, Cloud Monitoring alert policies. GPU/OpenStack unit economics: ⛔. |
| 9 drift (data, concept, agentic) | ✅ / ⛔ | Data/concept drift ✅. Agentic drift: tool-call mix and escalation rate are tracked; graph-edit-distance trajectory drift ⛔. |
| 10 edge cases | ✅ mostly | Crosstalk/DTMF/SIP-BYE (audio) ⛔; code-switching, sarcasm, outage storms (tower-level incident dedupe), injection, fraud/SIM-swap cues, BSS/OSS failure all covered by tests/evals. Voiceprint match ⛔. |
| 7.1 shadow pipeline | ⛔ | Not built; HITL sampling + the eval gate are the validation mechanisms. |
| 7.2 golden suite | 🔁 | Replaced by the eval runner with **release gates** (`evals/thresholds.yaml`), tau2 held-out test split, and a synthetic suite. |
| 7.4 chaos | ✅ | `telecom-sim /admin/chaos`, `fail_with` tools, "backend down" eval, DLQ/kill-switch tests. |
| 12 governance | ✅ / 🔶 | Attributed claims, decision records (`actions_audit`), RBAC, CMEK option, mTLS/IAM. Demographic WER parity ⛔ (needs audio + labelled demographics). |

## What v2 adds beyond the specification
* **Typed contracts and a deterministic policy engine** between "AI proposed" and "money moved".
* **Prompt-injection and fraud quarantine** with a leave-one-family-out evaluation of the injection model.
* **Idempotent, replay-safe action execution**, human approval workflow, global kill switch, deferred reconcile.
* **Honest evaluation harness**: models trained on the tau2 *train* split only, scored on the official *test* split; every suite states what its numbers do and do not show; release gates.
* **Infra generated from code** (BigQuery schemas, Pub/Sub topology) with consistency tests; canary release with automatic rollback.
* **Value model** with every assumption exposed (`ASSUMPTIONS`) instead of an unexplained ROI figure.
