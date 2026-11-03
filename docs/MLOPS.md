# MLOps and guardrails

## Lifecycle
1. **Data** – `data/corpus` (tau2) + IBM churn + generated synthetic; hashed into `MANIFEST.json` (`data_hash`).
2. **Train** – `make train` (production bundle) / `make train-eval` (train split only). Deterministic seeds; ~4 min on CPU. The Docker build trains inside the image, so the image digest pins code + models.
3. **Evaluate** – `make eval` runs 11 suites and enforces `evals/thresholds.yaml` (79 gates at the last run, all passing). Gates were set from measured behaviour with margin and are deliberately loose where the metric is noisy – they catch regressions, they do not certify accuracy.
4. **Release** – Cloud Build: tests → image → canary (10 %) → `canary_check.py` → promote/rollback.
5. **Monitor** – drift (PSI/JSD/KS/MMD/OOV, low-confidence share, injection rate), outage detector, HITL sampling statistics, Prometheus metrics + Cloud Monitoring alerts.
6. **Learn** – HITL verdicts (`/v1/hitl/*`) accumulate in `hitl_reviews`. A retraining job that consumes them and promotes models automatically is **not built**; today a human triggers `make train`, reviews the gated eval, and releases.

## Guardrails (defence in depth)
| Layer | Mechanism |
|---|---|
| Input | redaction, injection scoring, tiering to `SAFE_MODE` |
| Reasoning | closed hypothesis set, evidence-required claims, grounding verifier, LLM call/time/budget caps |
| Proposal | typed `ActionRequest`s with evidence ids |
| Decision | deterministic policy in the broker (never trusts the proposer) |
| Execution | idempotent connectors, approvals, kill switch, deferral/reconcile |
| Audit | append-only `actions_audit`, decision records, re-identification log |

## Value model
`value/model.py` converts churn probability, contract type and offer terms into expected retained margin (`ASSUMPTIONS`: gross margin 0.55, lifetime cap 36 months, monthly hazards by contract, …). Override via `CONVOIQ_VALUE_ASSUMPTIONS`. Outputs are expectations on synthetic cohorts; validate with a controlled experiment before using for budgeting.
