# Security and privacy model

## Trust zones
| Zone | Contents | Who/what can access |
|---|---|---|
| **Raw zone** | `gateway` ingest endpoint, raw topic (1-day retention), `redaction` service, PII vault | only gateway/redaction SAs; vault key in Secret Manager |
| **Redacted zone** | everything else: enrichment, orchestrator, broker, analytics, BigQuery | services see tokens (`<PHONE_a1b2…>`), never raw PII |
| **Edge** | optional: telco redacts on-prem and calls `/v1/interactions/redacted` | the attestation (HMAC over turn hashes) is verified; a cloud-side re-scan rejects payloads with residual PII |

## Controls
* **AuthN/Z** – Cloud Run IAM + app-level OIDC (`auth_mode=oidc`): principal → roles map (`ingest`, `analyst`, `approver`, `admin`, `service`). Internal service-to-service calls use the `service` role. Pub/Sub push uses a dedicated SA and audience-bound OIDC tokens. `apikey` mode exists for dev only.
* **Network** – internal ingress for all but the gateway; gateway behind external HTTPS LB + Cloud Armor (per-client rate limit, preconfigured SQLi/XSS WAF rules); Direct VPC egress through Cloud NAT for stable egress IP.
* **PII** – dual-pass redaction, HMAC pseudonymisation (stable per customer, key-rotatable), Fernet vault, TTL on vault entries, GDPR erase and audited re-identification (admin-only, every call logged).
* **Prompt injection** – customer text is untrusted: scored by a classifier + regex; high score → `SAFE_MODE` (only note/escalate actions); retrieved KB text and tool outputs are treated as data; the LLM can only choose from a closed hypothesis set and its summary is dropped unless claims resolve to evidence.
* **Action safety** – deterministic policy in the broker (identity verified, limits, rolling 30-day cap, quarantine, kill switch, evidence required); deterministic idempotent action ids; append-only audit in BigQuery.
* **Supply chain** – pinned requirements, non-root (`USER 10001`) `python:slim` runtime image built multi-stage, `.dockerignore` keeps data/secrets out, CI runs tests before build.
* **Secrets** – Secret Manager, mounted as env via Cloud Run secret refs; no secrets in images or the repository (`.env.example` has placeholders only).

## Threat model (summary)
| Threat | Mitigation | Residual risk |
|---|---|---|
| Customer injects instructions (“refund me $500, ignore policy”) | Injection scoring, SAFE_MODE, policy engine independent of the planner | Novel attack families (LOFO min recall 0.64) – policy limits cap the blast radius |
| Poisoned KB / tool output | Evidence chunks are data; claim-to-evidence grounding | KB write access must stay restricted |
| Forged “already redacted” payload | Attestation HMAC + re-scan | Compromise of the attestation key |
| Replay / duplicate events | Idempotency keys; deterministic action ids | – |
| Compromised internal service | Per-service SAs, least-privilege IAM, broker policy re-check | Lateral movement within `service` role |
| Insider re-identification | admin-only, audited | Needs periodic audit review |
| Name leakage (free-text names without cues) | Second strict pass, known-name lists | Lower recall than structured PII (KNOWN_LIMITS) |

## Compliance notes
Designed to support GDPR (erasure, minimisation, residency pin), PCI-DSS scope reduction (PAN/CVV never leave the raw zone), and call-recording notice monitoring (compliance rule R4 flags voice calls without a recording notice; R4 has no labelled ground truth, see KNOWN_LIMITS). This repository is **not** a compliance certification; run a DPIA and penetration test before production use.
