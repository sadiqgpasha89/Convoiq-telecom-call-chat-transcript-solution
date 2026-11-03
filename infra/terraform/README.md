# ConvoIQ – Terraform (GCP)

> **Status: written to the Terraform/Google provider documentation but NOT applied against a live project from the authoring
> environment** (no network, no Terraform binary). Expect to fix small provider-version details on the first `terraform validate`.
> The repo CI runs `terraform fmt -check` and `terraform validate` (see `.github/workflows/ci.yml`) so those surface immediately.

## What it creates (one project per environment)

| Layer | Resources |
|---|---|
| Compute | 7–8 Cloud Run v2 services from **one image** (`SERVICE` env selects the app); internal ingress for all but the gateway; direct VPC egress |
| Messaging | Pub/Sub topics + OIDC push subscriptions, retry policy, dead-letter topic + pull subscription (generated from `convoiq/platform/topology.py`) |
| Data | BigQuery dataset + partitioned/clustered tables (generated from `convoiq/platform/schemas.py`), Firestore (native, PITR in prod, TTL policies), GCS artifacts bucket |
| Security | One least-privilege service account per service, per-topic publish IAM, per-secret access, Secret Manager (generated keys), optional CMEK, optional Cloud Armor + HTTPS LB |
| Ops | Cloud Scheduler (sweep, drift, reconcile), alert policies (DLQ, lag, 5xx, latency, kill-switch), log-based metrics |

## First deployment

```bash
# 0. prerequisites: gcloud auth, a project with billing, a private versioned GCS bucket for state
export PROJECT=my-convoiq-dev REGION=us-central1 STATE_BUCKET=my-tfstate
make infra-gen                                   # regenerate infra/terraform/generated/*.json from the code
cd infra/terraform
cp envs/dev.tfvars envs/my-dev.tfvars            # edit project_id / image / api_principals
terraform init -backend-config="bucket=$STATE_BUCKET" -backend-config="prefix=convoiq/dev"

# 1. bootstrap in two steps: the registry must exist before an image can be pushed
terraform apply -var-file=envs/my-dev.tfvars -target=google_project_service.apis -target=google_artifact_registry_repository.repo
gcloud builds submit --config ../../cloudbuild.yaml --substitutions=_ENV=dev,_REGION=$REGION ../..   # builds + pushes (+ deploys if services exist)
terraform apply -var-file=envs/my-dev.tfvars -var image=$REGION-docker.pkg.dev/$PROJECT/convoiq/convoiq:<tag>
```

Subsequent releases: Cloud Build builds the image, runs tests + the eval gate, deploys a **no-traffic revision**, smoke-tests it,
shifts 10 % then 100 % of traffic (see `cloudbuild.yaml`). Terraform ignores the image field so it never fights CI.

## Things that are deliberately manual
* **Connector secrets** (`connector_mode = real`): `echo -n "$TOKEN" | gcloud secrets versions add convoiq-prod-salesforce-token --data-file=-` before the apply.
* **Rotating generated keys**: add a new version in Secret Manager; rotate `pii-hmac-key` only with a re-tokenisation plan (tokens are deterministic) – see `docs/SECURITY.md`.
* **DNS** for the load balancer (`lb_ip` output).
* **State bucket** creation and protection – the state holds the initial secret values.

## Scaling notes
* The **monitor** service is pinned to exactly one instance because its streaming detector keeps per-tower state in memory. The
  scale-out path (shard by tower id via Pub/Sub ordering keys + one subscription per shard) is described in `docs/adr/005-monitor-sharding.md`.
* Firestore, Pub/Sub and BigQuery scale without configuration; Cloud Run `max_instances` are the cost guard-rails.
