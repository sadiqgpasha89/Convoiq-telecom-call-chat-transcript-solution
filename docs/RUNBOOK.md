# Operations runbook

Variables used below: `ENV=prod`, `P=<gcp project>`, `R=<region>`; resource names are `${ENV}-convoiq-<service>` and topics/subscriptions are prefixed `${ENV}-`.

## First deploy
1. `cd infra/terraform && terraform init -backend-config=...` (state bucket: private, versioned, CMEK; it will hold generated secrets – see KNOWN_LIMITS).
2. `terraform apply -var-file=envs/dev.tfvars -target=google_project_service.apis` (enable APIs), then a full `apply`.
3. `gcloud builds submit --config cloudbuild.yaml` (builds the image – models are trained inside the build from the committed corpus – and runs `scripts/release.sh`: deploy consumers first, canary at 10 %, `scripts/canary_check.py`, promote or roll back).
4. Smoke: `python scripts/demo.py --base $(terraform output -raw gateway_url) --key $(gcloud auth print-identity-token)  # needs the telecom-sim reachable; pass --sim`.
> None of steps 1–4 were executed in the authoring sandbox. Treat the first dev deploy as a test of the infra code.

## Kill switch (freeze all automated side-effects)
```
curl -X POST $BROKER/admin/kill-switch -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$BROKER)" \
     -H 'content-type: application/json' -d '{"enabled": true, "reason": "INC-1234"}'
```
Requires the `admin` role. While enabled, the broker refuses every action except CRM notes; analysis continues. Disable with `"enabled": false`. A static override exists as env `CONVOIQ_KILL_SWITCH=true` (needs a redeploy).

## Dead-letter replay
Alert: *dead-letter backlog* (undelivered messages > 0 for 10 min).
1. Inspect: `gcloud pubsub subscriptions pull ${ENV}-dead-letter-pull --limit=10 --auto-ack=false --format=json`. Attributes `CloudPubSub-DeadLetterSourceSubscription` / `…DeliveryAttempt` identify origin; poison messages that services published themselves carry an `error` field.
2. Classify: *permanent* (malformed / failed redaction attestation) → fix the producer, discard. *Transient* (downstream outage) → replay.
3. Replay: republish the `data` and `ordering_key` of each message to the **source topic** (derived from the source subscription name, e.g. `redaction-raw` → `convoiq-interactions-raw`), then `ack` it on the pull subscription. Handlers are idempotent so double replay is safe.
4. Never replay raw-topic messages older than 24 h – the raw topic retains 1 day by design.

## Incident: PII found downstream
1. Flip the kill switch; stop the `enrichment` push subscription (`gcloud pubsub subscriptions modify-push-config … --push-endpoint=""` pauses delivery).
2. Identify tokens/interaction ids from the `residual_findings` / cloud re-scan alert; call `POST $REDACTION/v1/erase` with the customer id (admin) for GDPR erasure of the vault entries.
3. Purge affected BigQuery partitions (`bq query` DELETE by `interaction_id`), file a privacy incident, add a regression case in `tests/v2` and a detector rule in `privacy/detectors.py`.

## Outage alert (tower incident)
`monitor` publishes `incident.opened` with an `OSS_TICKET` proposal. If the detector is noisy: check `GET $MONITOR/v1/incidents`; tune `OUTAGE_*` env vars; false alarms are deduplicated per tower for the incident window. After monitor restarts, detection may lag for one window while state is rebuilt from `interactions_fact`.

## Drift alert
`POST $MONITOR/v1/drift/run` (Scheduler hourly/daily) compares the live window to `models/reference_distributions.json`. `GET /v1/drift/latest` shows PSI / JSD / KS / MMD(p) per signal. A *warn* is investigated; a *alert* with a shifted label mix or OOV spike → check upstream ASR/vendor changes, sample transcripts via HITL, retrain (`make train`) and run `make eval` gates before promoting.

## Releasing a new model or code version
`make train-eval && make eval` must pass all gates in `evals/thresholds.yaml`; commit; Cloud Build runs tests, builds, deploys with a 10 % canary and `scripts/canary_check.py` (error rate, p95 latency, health); automatic rollback on failure.

## Approvals backlog
`GET $GATEWAY/v1/approvals` (role `approver`) lists `PENDING_APPROVAL` actions; `POST /v1/approvals/{action_id}` `{"approve": true|false, "note": "..."}` decides. Unapproved credits above the auto limit never execute; stale approvals are not auto-approved.

## CI permissions
The Cloud Build SA needs: `roles/run.admin`, `roles/iam.serviceAccountUser` on the runtime SAs, `roles/artifactregistry.writer`. GitHub Actions (`ci/github-actions-ci.yml`, copied to `.github/workflows/ci.yml`) runs lint, tests, `make infra-check` (generated infra files are current) and `terraform fmt/validate` without credentials.
