# ---------------------------------------------------------------------------------------------------------------------
# Cloud Run v2 services. One image, SERVICE selects the app. Internal services accept only INTERNAL traffic (Pub/Sub, Scheduler,
# VPC callers); the gateway is the single public entry point.
# ---------------------------------------------------------------------------------------------------------------------
locals {
  sizing = {
    gateway       = { cpu = "1", memory = "1Gi", concurrency = 80, timeout = 60 }
    redaction     = { cpu = "1", memory = "1Gi", concurrency = 40, timeout = 60 }
    enrichment    = { cpu = "2", memory = "2Gi", concurrency = 16, timeout = 120 }
    orchestrator  = { cpu = "2", memory = "2Gi", concurrency = 8, timeout = 300 }
    action-broker = { cpu = "1", memory = "512Mi", concurrency = 40, timeout = 120 }
    analytics     = { cpu = "1", memory = "1Gi", concurrency = 40, timeout = 120 }
    monitor       = { cpu = "1", memory = "1Gi", concurrency = 40, timeout = 120 }
    telecom-sim   = { cpu = "1", memory = "512Mi", concurrency = 80, timeout = 30 }
  }

  # OIDC principals recognised by the apps: internal service accounts (role "service") + operator-supplied callers with roles
  internal_principals = join(",", concat(
    [for s in local.services : google_service_account.svc[s].email],
    [google_service_account.pubsub.email, google_service_account.scheduler.email]
  ))
  external_principals = join(",", [for e, r in var.api_principals : "${e}:${r}"])

  common_env = {
    CONVOIQ_ENV                    = var.env
    CONVOIQ_GCP_PROJECT            = var.project_id
    CONVOIQ_GCP_REGION             = var.region
    CONVOIQ_BIGQUERY_DATASET       = google_bigquery_dataset.lake.dataset_id
    CONVOIQ_PUBSUB_TOPIC_PREFIX    = local.topic_prefix
    CONVOIQ_BUS_BACKEND            = "pubsub"
    CONVOIQ_STORE_BACKEND          = "firestore"
    CONVOIQ_WAREHOUSE_BACKEND      = "bigquery"
    CONVOIQ_AUTH_MODE              = "oidc"
    CONVOIQ_LLM_PROVIDER           = var.llm_provider
    CONVOIQ_LLM_MODEL              = var.llm_model
    CONVOIQ_LLM_DAILY_BUDGET_USD   = tostring(var.llm_daily_budget_usd)
    CONVOIQ_CONNECTOR_MODE         = var.connector_mode
    CONVOIQ_REDACTION_URL          = local.run_url["redaction"]
    CONVOIQ_ENRICHMENT_URL         = local.run_url["enrichment"]
    CONVOIQ_ORCHESTRATOR_URL       = local.run_url["orchestrator"]
    CONVOIQ_ACTION_BROKER_URL      = local.run_url["action-broker"]
    CONVOIQ_ANALYTICS_URL          = local.run_url["analytics"]
    CONVOIQ_MONITOR_URL            = local.run_url["monitor"]
    CONVOIQ_TELECOM_SIM_URL        = var.connector_mode == "simulated" ? local.run_url["telecom-sim"] : ""
    CONVOIQ_SALESFORCE_URL         = var.salesforce_url
    CONVOIQ_SERVICENOW_URL         = var.servicenow_url
    CONVOIQ_BILLING_URL            = var.billing_url
  }

  secret_env = {
    CONVOIQ_PII_HMAC_KEY    = "pii-hmac-key"
    CONVOIQ_ATTESTATION_KEY = "attestation-key"
  }
}

resource "google_cloud_run_v2_service" "svc" {
  for_each            = toset(local.services)
  name                = "${local.prefix}-${each.key}"
  location            = var.region
  ingress             = each.key == "gateway" ? (var.enable_lb ? "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" : "INGRESS_TRAFFIC_ALL") : "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = var.env == "prod"
  launch_stage        = "GA"

  template {
    service_account                  = google_service_account.svc[each.key].email
    timeout                          = "${local.sizing[each.key].timeout}s"
    max_instance_request_concurrency = local.sizing[each.key].concurrency
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"

    scaling {
      # monitor keeps its streaming detector state in memory -> exactly one instance (sharding by tower id is the scale-out path, see ADR-005)
      min_instance_count = each.key == "monitor" ? 1 : lookup(var.min_instances, each.key, 0)
      max_instance_count = each.key == "monitor" ? 1 : lookup(var.max_instances, each.key, 5)
    }

    # direct VPC egress so internal-ingress services can call each other; ALL_TRAFFIC because *.run.app resolves to Google public IPs
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }

    containers {
      image = var.image
      resources {
        limits            = { cpu = local.sizing[each.key].cpu, memory = local.sizing[each.key].memory }
        cpu_idle          = each.key != "monitor" # monitor must keep CPU between requests (background sweeps)
        startup_cpu_boost = true
      }
      ports { container_port = 8080 }

      env {
        name  = "SERVICE"
        value = replace(each.key, "-", "_")
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name  = "CONVOIQ_SERVICE_NAME"
        value = each.key
      }
      env {
        # audience a caller must mint its ID token for = this service's URL
        name  = "CONVOIQ_OIDC_AUDIENCE"
        value = local.run_url[each.key]
      }
      env {
        name  = "CONVOIQ_ALLOWED_SERVICE_ACCOUNTS"
        value = each.key == "gateway" ? join(",", compact([local.internal_principals, local.external_principals])) : local.internal_principals
      }
      dynamic "env" {
        for_each = each.key == "redaction" ? { CONVOIQ_VAULT_KEY = "vault-key" } : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = local.secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      dynamic "env" {
        for_each = (each.key == "action-broker" && var.connector_mode != "simulated") ? {
          CONVOIQ_SALESFORCE_TOKEN = "salesforce-token", CONVOIQ_SERVICENOW_TOKEN = "servicenow-token", CONVOIQ_BILLING_TOKEN = "billing-token"
        } : {}
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.connector[env.value].secret_id
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 40 # models load in the background of the first seconds; allow ~2 min
      }
      liveness_probe {
        http_get { path = "/healthz" }
        period_seconds    = 20
        failure_threshold = 3
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  labels = { env = var.env, app = "convoiq", service = each.key }

  lifecycle {
    # Cloud Build deploys new revisions with `gcloud run deploy`; Terraform owns shape, CI owns the image tag
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_project_service.apis, google_secret_manager_secret_version.v]
}
