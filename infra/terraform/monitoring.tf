# Alerting on the symptoms operators act on (SLO definitions and burn-rate reasoning: docs/SLOS.md).
resource "google_monitoring_notification_channel" "email" {
  for_each     = toset(var.alert_emails)
  display_name = "ConvoIQ ${var.env} ${each.value}"
  type         = "email"
  labels       = { email_address = each.value }
}
locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

# 1. Anything in the dead-letter queue is a poison or persistently failing message: investigate (runbook: DLQ replay).
resource "google_monitoring_alert_policy" "dlq_backlog" {
  display_name = "[${var.env}] ConvoIQ dead-letter backlog"
  combiner     = "OR"
  conditions {
    display_name = "undelivered DLQ messages > 0 for 10m"
    condition_threshold {
      filter          = "resource.type=\"pubsub_subscription\" AND resource.labels.subscription_id=\"${google_pubsub_subscription.dlq_pull.name}\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "600s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = local.channels
  documentation {
    content   = "Messages reached the DLQ after ${local.topology.max_delivery_attempts} delivery attempts. See docs/RUNBOOK.md#dead-letter-replay."
    mime_type = "text/markdown"
  }
}

# 2. Pipeline lag: the oldest unacked message on any push subscription is older than 10 minutes.
resource "google_monitoring_alert_policy" "pipeline_lag" {
  display_name = "[${var.env}] ConvoIQ pipeline lag"
  combiner     = "OR"
  conditions {
    display_name = "oldest unacked message > 600s"
    condition_threshold {
      filter          = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/oldest_unacked_message_age\" AND metadata.system_labels.name=starts_with(\"${local.topic_prefix}\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 600
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  notification_channels = local.channels
}

# 3. Gateway 5xx ratio (availability SLO 99.9% -> page when >2% of requests fail for 5 minutes).
resource "google_monitoring_alert_policy" "gateway_5xx" {
  display_name = "[${var.env}] ConvoIQ gateway 5xx ratio"
  combiner     = "OR"
  conditions {
    display_name = "5xx / all > 2%"
    condition_threshold {
      filter     = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.prefix}-gateway\" AND metric.type=\"run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx\""
      denominator_filter = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.prefix}-gateway\" AND metric.type=\"run.googleapis.com/request_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.02
      duration        = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  notification_channels = local.channels
}

# 4. Gateway latency (p95 > 2s for 10 minutes).
resource "google_monitoring_alert_policy" "gateway_latency" {
  display_name = "[${var.env}] ConvoIQ gateway p95 latency"
  combiner     = "OR"
  conditions {
    display_name = "p95 request latency > 2s"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${local.prefix}-gateway\" AND metric.type=\"run.googleapis.com/request_latencies\""
      comparison      = "COMPARISON_GT"
      threshold_value = 2000
      duration        = "600s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  notification_channels = local.channels
}

# 5. Kill switch engaged / actions blocked: log-based metric -> alert (a human flipped the switch, or the policy engine is blocking a lot).
resource "google_logging_metric" "kill_switch" {
  name   = "${local.prefix}-kill-switch-changed"
  filter = "resource.type=\"cloud_run_revision\" AND jsonPayload.message=\"kill switch changed\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}
resource "google_monitoring_alert_policy" "kill_switch" {
  display_name = "[${var.env}] ConvoIQ kill switch changed"
  combiner     = "OR"
  conditions {
    display_name = "kill switch toggled"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.kill_switch.name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  notification_channels = local.channels
  alert_strategy { auto_close = "3600s" }
}

# 6. Drift: monitor logs the recommendation each hour; RETRAIN_CANDIDATE is a ticket, not a page.
resource "google_logging_metric" "drift_retrain" {
  name   = "${local.prefix}-drift-retrain-candidate"
  filter = "resource.type=\"cloud_run_revision\" AND jsonPayload.message=\"drift evaluated\" AND jsonPayload.recommendation:\"RETRAIN\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}
