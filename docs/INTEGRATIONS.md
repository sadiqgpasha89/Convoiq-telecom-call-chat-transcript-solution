# Integrations

`CONNECTOR_MODE` selects what the action broker talks to: `simulated` (the in-repo `telecom-sim`), `dryrun` (log only) or `real`.

| System | Interface | Status |
|---|---|---|
| BSS/OSS/CRM simulator (`telecom-sim`) | REST: customers, bills, towers, credits, offers, tickets, notes, escalations, callbacks, chaos injection | ✅ tested end-to-end |
| Salesforce (cases/notes) | sObject REST shape | 🔶 scaffold, **not run against a live org** |
| ServiceNow (incidents) | Table API shape | 🔶 scaffold, **not run against a live instance** |
| Billing/BSS credits and offers | generic REST with `Idempotency-Key` = `action_id` | 🔶 scaffold; map to your BSS API |
| LLM (Vertex AI Gemini / Anthropic) | `agents/llm.py` adapters behind `GuardedLLM` | 🔶 adapters untested live; default `llm_provider=none` |
| Ingest | REST (single, batch, edge-redacted) → Pub/Sub | ✅ |
| Speech-to-text | **not included** – feed text transcripts (e.g., Google Speech-to-Text with diarisation, Contact Center AI Insights export) | ⛔ |
| BI | BigQuery tables (`platform/schemas.py`); connect Looker Studio/Looker | ⛔ dashboards not built |

## Writing a connector
Implement `Connector` (`supports`, `execute`) so that `execute` is idempotent on `action_id`, returns `retryable=True` for transient failures (the broker then defers and reconciles) and never raises on business errors. Wire it into `connectors/base.py:make_router`/`ConnectorRouter`, add contract tests mirroring the simulator-backed ones in `tests/v2/`.
