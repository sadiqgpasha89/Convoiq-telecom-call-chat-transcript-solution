# ConvoIQ v2 – Architecture

ConvoIQ turns telecom **call and chat transcripts** into decisions: what broke, who is at risk of leaving, which agent conduct rules were
violated, and which downstream actions (credit, ticket, retention offer, escalation) are justified – with every claim traceable to a
transcript turn, a knowledge-base chunk, a model, or a system-of-record lookup.

## 1. Design principles

1. **Privacy is structural, not a policy.** Only redacted, tokenised, *attested* text crosses into the analytics plane. The raw→redacted
   boundary is its own service; the gateway re-verifies the attestation, and a cloud-side re-scan rejects forged "clean" payloads.
2. **Deterministic by default, LLM optional.** Every decision has a deterministic path (classical ML + rules + retrieval). An LLM can only
   (a) break near-ties between hypotheses from a *closed set* and (b) rewrite the summary – and the rewrite is accepted only if every claim
   resolves to real evidence (`guardrails/grounding.py`). With `llm_provider=none` the system is complete and reproducible.
3. **AI proposes, policy disposes.** Agents emit *proposals*. The **action broker** re-evaluates a deterministic policy
   (`guardrails/policy.py`) that does not trust the proposer: credit limits, rolling caps, identity checks, quarantine rules, kill switch,
   evidence required, human approval tiers. An injected or hallucinated proposal cannot exceed what the policy would allow anyway.
4. **At-least-once everywhere, exactly-once effects.** Pub/Sub redelivers; every consumer is idempotent (event-id keys in Firestore), and
   the broker uses deterministic action ids (`ActionRequest.build`) so a replayed event cannot repeat a credit.
5. **Degrade, never stall.** BSS/OSS outage → circuit breaker → analysis completes from transcript + models and the broker *defers* side
   effects to a reconcile job. A failed optional agent node degrades the result and says so (`Analysis.degraded`).
6. **Everything the code owns generates the infra.** Pub/Sub wiring and BigQuery schemas live in code (`platform/topology.py`,
   `platform/schemas.py`); `scripts/gen_infra.py` emits the Terraform inputs and a test fails if they drift.

## 2. Runtime topology (GCP)

```
 partner / edge ──HTTPS+OIDC──▶ [gateway] ──publish──▶ interactions-raw ──push──▶ [redaction] ──▶ interactions-redacted
                                   │  ▲ (edge-redacted payloads: attestation verified)                     │ push
                                   │  └───────────────────────────────────────────────────────────────────┘
                                   ▼                                                                       ▼
                         read APIs (insights, approvals)                                              [enrichment]
                                   │                                                                       │ publish interactions-enriched
                            [analytics] ◀── push (enriched, analyzed, actions-result, incidents)  ◀───────┤
                                   ▲                                                       ┌──────────────┼───────────────┐
                                   │                                                       ▼              ▼               ▼
                                   │                                              [orchestrator]     [monitor]      [analytics]
                                   │                         analyzed ◀── publish ───┘  │ publish         │ publish incidents
                                   │                                                    ▼ actions-proposed ▼  + actions-proposed
                                   └──── actions-result ◀── [action-broker] ◀───────────┴──────────────────┘
                                                                  │ policy ▸ HITL approval ▸ connectors
                                                                  ▼
                                             Salesforce / ServiceNow / BSS  (or [telecom-sim] in dev)
 Cloud Scheduler ▸ monitor /v1/sweep, /v1/drift/run ▸ broker /v1/reconcile        BigQuery ◀ analytics, broker, monitor
 Firestore: idempotency, status, approvals, vault, kill-switch                      Secret Manager: HMAC / attestation / vault keys
```

| Service | Responsibility | State |
|---|---|---|
| `gateway` | Public API: ingest (raw or edge-redacted), read insights, approvals; RBAC; idempotency-key dedupe | Firestore (dedupe) |
| `redaction` | Detect + tokenise PII/PCI (dual pass), sign attestation, vault, GDPR erasure, audited re-identification | Firestore (vault) |
| `enrichment` | Issue classification, entities, sentiment arc, churn cues, embeddings, injection/fraud signals, tiering | stateless (models in image) |
| `orchestrator` | Agent mesh (DAG): context → diagnosis → compliance → QA → churn → summary → action planning; HITL sampling | stateless + LLM budget in Firestore |
| `action-broker` | Policy gate, approvals, kill switch, connector execution, deferral + reconcile, audit | Firestore + BigQuery `actions_audit` |
| `analytics` | Sinks to BigQuery fact tables; insight queries | BigQuery |
| `monitor` | Outage detector (Poisson + EWMA + CUSUM per tower), drift evaluation | in-memory per-tower state, **single instance** (ADR-005) |
| `telecom-sim` | Simulated BSS/OSS/CRM for dev, staging and tests (never deployed with real connectors) | Firestore |

All services are one Docker image; `SERVICE` selects the FastAPI app. Cloud Run, not GKE (ADR-001).

## 3. Per-interaction data flow

1. **Ingest** – `POST /v1/interactions` (raw, redaction in-zone) or `/v1/interactions/redacted` (edge-redacted + attestation).
2. **Redact** – detectors (cue-aware: the previous agent turn promotes bare digits to PIN/DOB/phone) → HMAC tokens → Fernet vault.
   Second strict pass; `residual_findings` must be 0 or the payload is dead-lettered, never forwarded. Attestation = SHA-256 of turns, HMAC-signed.
3. **Enrich** – `models/bundle.py` loads: issue classifier (TF-IDF+LR, temperature-scaled), root-cause OvR, resolution-risk, churn (IBM telco HGB),
   injection (char-n-gram LR + regex), LSA embedder + case index. Tiering: routine → FAST_PATH (no swarm), complex/at-risk → SWARM, injection/fraud → SAFE_MODE.
4. **Analyse** (`agents/orchestrator.py`) – async DAG with per-node timeouts; optional nodes degrade instead of failing:
   `context` (BSS/OSS via circuit-broken tools) → `diagnose` (ML prior + case voting + KB evidence + authoritative verification + OSS-seeded hypotheses)
   → `compliance` (R1–R10) → `qa` (trajectory audit) → `churn` (tabular + conversation fusion, stated-intent floor) → `summary` (template; optional verified LLM) → `plan`.
5. **Act** – broker: policy → auto-execute / `PENDING_APPROVAL` / blocked / deferred; connector call with idempotency key; result event.
6. **Learn** – analytics tables, drift reports, HITL verdicts (`hitl_reviews`), outage incidents → Cloud Scheduler-driven monitors.

## 4. Agent modes

| Mode | Trigger | Behaviour |
|---|---|---|
| `FAST_PATH` | routine issue, high confidence, low risk | summary + CRM note only; no swarm, no LLM |
| `SWARM` | complex / at-risk / low confidence | full DAG |
| `SAFE_MODE` | injection score ≥ 0.5 or fraud score ≥ 0.6 | quarantine: only `CRM_NOTE`, `ESCALATE_FRAUD`, `ESCALATE_HUMAN` may be proposed (and the broker enforces the same) |

## 5. Failure handling

| Failure | Behaviour |
|---|---|
| Malformed event | `PermanentError` → published to DLQ topic, push acked (no redelivery storm) |
| Handler exception | HTTP 5xx → Pub/Sub retry with backoff → after 5 attempts dead-letter subscription → alert |
| BSS/OSS timeout | 800 ms budget, retry, circuit breaker (5 failures / 30 s); analysis continues, `degraded` listed |
| Connector outage | action `DEFERRED`; Scheduler → `/v1/reconcile` retries with the original idempotency key |
| LLM down / slow / hostile | `GuardedLLM`: call cap, timeout, daily budget; fail closed to the template path |
| Bad CRM data | churn agent falls back to the base-rate prior and records `crm_profile_invalid` |

## 6. Schema evolution
Events carry `schema_version` (see `contracts/`). Contracts are **additive-only** within a major version; `scripts/release.sh` deploys
consumers before producers so a new field is never emitted to a consumer that cannot parse it.

## 7. Multi-tenancy
`tenant_id` is on every event and table and a cluster key in BigQuery. Isolation today is *logical* (filters + IAM on services). Per-tenant
datasets/projects are the upgrade path for contractual isolation (see `docs/KNOWN_LIMITS.md`).

## 8. Where the original specification was adapted
See `docs/SCOPE.md` for the mapping from the v1 specification to what is built, what was replaced (and why) and what is out of scope.
