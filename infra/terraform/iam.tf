locals {
  prefix = "convoiq-${var.env}"

  # telecom-sim is a dev/staging stand-in for BSS/OSS/CRM - it is never deployed when real connectors are configured
  services = concat(
    ["gateway", "redaction", "enrichment", "orchestrator", "action-broker", "analytics", "monitor"],
    var.connector_mode == "simulated" ? ["telecom-sim"] : []
  )
  internal_services = [for s in local.services : s if s != "gateway"]
  topology          = jsondecode(file("${path.module}/generated/pubsub_topology.json"))
  bq_tables         = jsondecode(file("${path.module}/generated/bq_tables.json"))
  subscriber_services = distinct([for s in local.topology.subscriptions : s.service])
  pubsub_agent      = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# One service account per service (least privilege), plus one for Pub/Sub push and one for Cloud Scheduler.
resource "google_service_account" "svc" {
  for_each     = toset(local.services)
  account_id   = "cq-${var.env}-${each.key}"
  display_name = "ConvoIQ ${var.env} ${each.key}"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "pubsub" {
  account_id   = "cq-${var.env}-pubsub"
  display_name = "ConvoIQ ${var.env} Pub/Sub push identity"
  depends_on   = [google_project_service.apis]
}
resource "google_service_account" "scheduler" {
  account_id   = "cq-${var.env}-sched"
  display_name = "ConvoIQ ${var.env} Cloud Scheduler"
  depends_on   = [google_project_service.apis]
}

# --- Firestore (idempotency, status, approvals, vault) - every service uses the store -----------------------------------------
resource "google_project_iam_member" "firestore" {
  for_each = toset(local.services)
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.svc[each.key].email}"
}

# --- BigQuery: only the writers/readers of the warehouse -----------------------------------------------------------------------
locals {
  bq_services = ["analytics", "action-broker", "monitor", "orchestrator"]
}
resource "google_bigquery_dataset_iam_member" "editor" {
  for_each   = toset(local.bq_services)
  dataset_id = google_bigquery_dataset.lake.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.svc[each.key].email}"
}
resource "google_project_iam_member" "bq_jobs" {
  for_each = toset(local.bq_services)
  project  = var.project_id
  role     = "roles/bigquery.jobUser"
  member   = "serviceAccount:${google_service_account.svc[each.key].email}"
}

# --- Vertex AI (only if the LLM provider is Vertex) -----------------------------------------------------------------------------
resource "google_project_iam_member" "vertex" {
  count   = var.llm_provider == "vertex" ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.svc["orchestrator"].email}"
}

# --- Pub/Sub publish rights, per topic (from the same topology the code is tested against) --------------------------------------
locals {
  publisher_pairs = merge([
    for topic, pubs in local.topology.publishers : {
      for p in pubs : "${topic}|${p}" => { topic = topic, svc = p } if contains(local.services, p)
    }
  ]...)
}
resource "google_pubsub_topic_iam_member" "publisher" {
  for_each = local.publisher_pairs
  topic    = google_pubsub_topic.t[each.value.topic].id
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:${google_service_account.svc[each.value.svc].email}"
}

# --- Cloud Run invocation (IAM is gate 1; the app's own OIDC/RBAC check is gate 2) -------------------------------------------------
# internal services may be invoked by: Pub/Sub push identity, Cloud Scheduler, and the gateway.
locals {
  invokers = {
    for pair in setproduct(local.internal_services, ["pubsub", "scheduler", "gateway"]) :
    "${pair[0]}|${pair[1]}" => { svc = pair[0], who = pair[1] }
    if !(pair[1] == "pubsub" && !contains(local.subscriber_services, pair[0]))
    && !(pair[1] == "scheduler" && !contains(["monitor", "action-broker"], pair[0]))
    && !(pair[1] == "gateway" && !contains(["redaction", "enrichment", "orchestrator", "analytics", "action-broker"], pair[0]))
  }
  invoker_member = {
    pubsub    = "serviceAccount:${google_service_account.pubsub.email}"
    scheduler = "serviceAccount:${google_service_account.scheduler.email}"
    gateway   = "serviceAccount:${google_service_account.svc["gateway"].email}"
  }
}
resource "google_cloud_run_v2_service_iam_member" "internal_invoker" {
  for_each = local.invokers
  name     = google_cloud_run_v2_service.svc[each.value.svc].name
  location = var.region
  role     = "roles/run.invoker"
  member   = local.invoker_member[each.value.who]
}
# the orchestrator/broker call telecom-sim (simulated mode) and the broker calls nothing else; the orchestrator also calls it for context
resource "google_cloud_run_v2_service_iam_member" "sim_invoker" {
  for_each = var.connector_mode == "simulated" ? toset(["orchestrator", "action-broker"]) : toset([])
  name     = google_cloud_run_v2_service.svc["telecom-sim"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.svc[each.key].email}"
}
resource "google_cloud_run_v2_service_iam_member" "gateway_callers" {
  for_each = var.gateway_public ? toset([]) : toset(keys(var.api_principals))
  name     = google_cloud_run_v2_service.svc["gateway"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = can(regex("\\.gserviceaccount\\.com$", each.key)) ? "serviceAccount:${each.key}" : "user:${each.key}"
}
resource "google_cloud_run_v2_service_iam_member" "gateway_public" {
  count    = var.gateway_public ? 1 : 0
  name     = google_cloud_run_v2_service.svc["gateway"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Pub/Sub service agent: mint OIDC tokens as the push identity, dead-letter publish, source-subscription ack --------------------
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_agent
}
resource "google_pubsub_topic_iam_member" "dlq_agent_publisher" {
  topic  = google_pubsub_topic.t[local.dlq_topic].id
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}
resource "google_pubsub_subscription_iam_member" "agent_subscriber" {
  for_each     = google_pubsub_subscription.push
  subscription = each.value.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}
