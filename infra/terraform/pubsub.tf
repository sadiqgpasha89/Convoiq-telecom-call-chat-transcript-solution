# Topics, push subscriptions (OIDC-authenticated), dead-lettering and retry - all generated from convoiq/platform/topology.py.
locals {
  dlq_topic    = "convoiq-dead-letter"
  topic_prefix = "${var.env}-"
  # deterministic Cloud Run URL (https://<service>-<project-number>.<region>.run.app) - avoids a resource cycle with the env vars
  run_url = { for s in local.services : s => "https://${local.prefix}-${s}-${data.google_project.this.number}.${var.region}.run.app" }
}

resource "google_pubsub_topic" "t" {
  for_each = toset(local.topology.topics)
  name     = "${local.topic_prefix}${each.key}"
  # raw transcripts must never linger: 1-day retention on the raw topic; residency pinned to our region
  message_retention_duration = each.key == "convoiq-interactions-raw" ? "86400s" : "604800s"
  message_storage_policy { allowed_persistence_regions = [var.region] }
  kms_key_name = (each.key == "convoiq-interactions-raw" && var.cmek_key_id != "") ? var.cmek_key_id : null
  labels       = { env = var.env }
  depends_on   = [google_project_service.apis]
}

resource "google_pubsub_subscription" "push" {
  for_each = { for s in local.topology.subscriptions : s.name => s if contains(local.services, s.service) }
  name     = "${local.topic_prefix}${each.key}"
  topic    = google_pubsub_topic.t[each.value.topic].id

  ack_deadline_seconds       = each.value.ack_deadline_s
  enable_message_ordering    = true
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" } # never expire

  push_config {
    push_endpoint = "${local.run_url[each.value.service]}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.pubsub.email
      audience              = local.run_url[each.value.service]
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.t[local.dlq_topic].id
    max_delivery_attempts = local.topology.max_delivery_attempts
  }
}

# Pull subscription on the DLQ: operators (and the replay runbook) read from here; alerting watches its backlog.
resource "google_pubsub_subscription" "dlq_pull" {
  name                       = "${local.topic_prefix}dead-letter-pull"
  topic                      = google_pubsub_topic.t[local.dlq_topic].id
  ack_deadline_seconds       = 60
  message_retention_duration = "1209600s"
  expiration_policy { ttl = "" }
}
