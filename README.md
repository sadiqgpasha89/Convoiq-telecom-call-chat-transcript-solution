# ConvoIQ v2 — Telecom Call & Chat Transcript Intelligence

Microservices on GCP (Cloud Run · Pub/Sub · BigQuery · Firestore · Terraform) that turn support **call and chat transcripts** into
decisions: what broke, who will churn, which conduct rules were violated, and which actions (credit, ticket, retention offer, escalation)
are justified — each claim tied to evidence, each action gated by a deterministic policy and human approval above limits.

> **Status: engineering-complete, not production-proven.** Code, models, evaluation and tests run here. Terraform, Docker, Cloud Build,
> and real GCP/CRM connectors were **written and cross-checked but never executed** in the authoring sandbox. Read
> [docs/KNOWN_LIMITS.md](docs/KNOWN_LIMITS.md) before relying on any number below.

## What it does
| Plane | Capability |
|---|---|
| Privacy | Dual-pass PII/PCI redaction, HMAC tokens, encrypted vault, GDPR erase, signed attestation checked again in the cloud |
| Understanding | Issue/intent, root-cause hypotheses, sentiment arc, sarcasm cue, churn cues & competitors, code-switching, injection/fraud signals |
| Agents | DAG: context → diagnosis (ML + case votes + RAG + live BSS/OSS verification) → compliance (R1–R10) → QA → churn → summary → action plan; optional LLM behind guards |
| Actions | Policy engine (limits, caps, identity, quarantine, kill switch), approvals (HITL), idempotent connectors, deferral + reconcile, audit in BigQuery |
| Monitoring | Tower outage detector (Poisson + EWMA + CUSUM), drift (PSI/JSD/KS/MMD/OOV), HITL sampling, Prometheus + Cloud Monitoring alerts |
| Infra | Terraform for Cloud Run ×8, Pub/Sub (OIDC push, ordering, DLQ), BigQuery, Firestore, Secret Manager, Scheduler, Cloud Armor LB, NAT; Cloud Build canary release |

Data: **tau2-bench telecom** (MIT; 2,736 simulated conversations, official train/test task split) + **IBM Telco Churn** (Apache-2.0, 7,043 rows) + a
transparent synthetic generator for what no public dataset covers (PII, fraud, injection, outages). See [docs/DATA_CARD.md](docs/DATA_CARD.md).

## First step after pulling this delivery (two protected files)
The delivery tool is not allowed to overwrite `Makefile` or anything under `.github/`, so those two arrive under other names.
Your folder still has the **v1** `Makefile` until you do this (its backup is `legacy/Makefile.v1`):
```bash
mv Makefile.v2 Makefile                                  # v2 targets used throughout this README
mkdir -p .github/workflows && cp ci/github-actions-ci.yml .github/workflows/ci.yml
```

## Quickstart (local, no GCP)
```bash
make install                # requires Python >= 3.12
make train                  # ~4 min CPU -> models/   (models/ is also committed)
make test                   # pytest tests/v2 (118 tests, all passing at hand-off)
make run-local              # all services in ONE process; gateway on :8080 (auth off)
make demo                   # in another shell: pushes 60 synthetic conversations, prints insights
make eval                   # trains on tau2 TRAIN split, scores on TEST split, enforces release gates
docker compose up --build   # same stack on Pub/Sub + BigQuery + Firestore emulators (not run by the author)
```
## Deploy to GCP
```bash
python scripts/gen_infra.py                       # regenerate BigQuery/Pub/Sub inputs from code (CI fails if stale)
cd infra/terraform && terraform init && terraform apply -var-file=envs/dev.tfvars
gcloud builds submit --config cloudbuild.yaml     # test -> build (trains models) -> canary -> promote / rollback
```
Details, kill switch, DLQ replay and incident procedures: [docs/RUNBOOK.md](docs/RUNBOOK.md), [infra/terraform/README.md](infra/terraform/README.md).

## Results (held-out tau2 test tasks; models trained on train tasks only) — 79/79 release gates pass
| Metric | Value | Caveat |
|---|---|---|
| Issue macro-F1 / ECE | 0.997 / 0.004 | simulated customers; real-world will be lower |
| Root cause micro-F1 | 0.80 (any gold cause hit 0.99, exact set 0.14) | multi-cause tickets |
| Resolution AUC (final / first 40 % of the call) | 0.955 / 0.83 | |
| Injection false positives on 9,583 customer turns | 0.01 % | attack recall on unseen families as low as 0.64 (policy engine is the real defence) |
| Redaction recall / leakage (synthetic) | 1.00 / 0 | template-derived, optimistic; names rely on cues |
| Outage detection | 100 % detected, median delay 9.5 min, 0 false alarms | simulated streams |
| Latency (in-process) | p95 169 ms per conversation, ~10 conv/s/thread | not a load test |
| Weak spots | sentiment trend 0.69, sarcasm recall 0.56, fix-selection F1 0.43, churn recall 0.61 at 30 % ASR noise | published, gated at current levels |

Full report: [reports/EVAL_REPORT.md](reports/EVAL_REPORT.md). Business value figures are expected values under stated assumptions, **not** measured uplift.

## Repository map
```
convoiq/            package: contracts, privacy, nlp, models, agents, guardrails, services (8), platform, evals, value, runtime
data/               corpus (tau2, gz), kb, reference (IBM churn), samples, NOTICE.md (licenses)
models/             production bundle (all data) + MANIFEST.json           (models_eval/ is regenerated by `make train-eval`)
infra/terraform/    GCP infrastructure (+ generated/ inputs)               scripts/  gen_infra, release, canary, demo, model cards
evals/thresholds.yaml   release gates      tests/v2/   test suite      reports/   latest evaluation
docs/               ARCHITECTURE, SCOPE, KNOWN_LIMITS, RUNBOOK, SECURITY, DATA_CARD, MODEL_CARDS, SLOS, INTEGRATIONS, MLOPS, adr/
legacy/             your previous README / Makefile / pyproject / requirements / .env.example (v1), preserved
src/ + old tests    LEGACY v1 prototype (see below)
```

## Legacy v1 prototype (`src/`, `tests/test_*.py`)
v1 (monolithic FastAPI prototype with simulated components and a browser UI) is **left in place** because the delivery tooling could not
delete files on your machine. v2 does not import it. Two things v1 had that v2 does **not** replace:
the browser UI in `src/frontend/` (v2 exposes APIs and BigQuery tables; connect Looker Studio or build a console), and the v1 module
stubs for Spanner/Vertex vector search (v2 uses Firestore/BigQuery and its own retriever — ADR-002/003).

Once you are satisfied, remove v1 (run from the repo root; `git rm` instead of `rm` if tracked):
```bash
rm -rf src tests/conftest.py tests/test_api_endpoints.py tests/test_data_engineering.py tests/test_datascience_models.py \
       tests/test_hybrid_rag.py tests/test_metrics_engine.py tests/test_mlops_llmops.py tests/test_multi_agent_swarm.py \
       convoiq_telecom_intelligence.egg-info .coverage uv.lock .pytest_cache
```
(`uv.lock` is stale for v2's dependencies; regenerate with `uv lock` if you use uv.) The original specification is unchanged;
[docs/SCOPE.md](docs/SCOPE.md) maps each of its areas to what was built, replaced or left out.

## Honest caveats (short list)
1. tau2 data is **LLM-simulated**; synthetic suites are **template-derived** — both overstate real-world accuracy.
2. Terraform / Docker / Cloud Build / release scripts / compose stack: **not executed**. Real Salesforce, ServiceNow, billing, Vertex and Anthropic adapters: **not exercised live**.
3. Compliance rules R4, R5, R7, R8, R9, R10 have no ground-truth labels anywhere; they are covered by mutation tests only.
4. Outage monitor is single-instance; tenant isolation is logical; `FirestoreVault` uses a sync client; Terraform state will contain generated secrets.
5. No audio path (ASR, diarisation); no shadow deployment or automated retraining promotion.

License: Apache-2.0 as declared in `pyproject.toml` (no LICENSE file is included yet — add one); third-party data under their own licenses (`data/NOTICE.md`).
