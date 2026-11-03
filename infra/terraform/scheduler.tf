# Cloud Scheduler -> OIDC calls to internal services (the scheduler SA holds run.invoker on exactly these two services).
locals {
  jobs = {
    "monitor-sweep"   = { svc = "monitor", path = "/v1/sweep", cron = "*/5 * * * *", desc = "close quiet network incidents" }
    "monitor-drift"   = { svc = "monitor", path = "/v1/drift/run", cron = "7 * * * *", desc = "hourly drift evaluation" }
    "broker-reconcile" = { svc = "action-broker", path = "/v1/reconcile", cron = "*/10 * * * *", desc = "retry DEFERRED actions after connector outages" }
  }
}

resource "google_cloud_scheduler_job" "j" {
  for_each    = local.jobs
  name        = "${local.prefix}-${each.key}"
  description = each.value.desc
  schedule    = each.value.cron
  time_zone   = "Etc/UTC"
  region      = var.region

  retry_config {
    retry_count          = 2
    min_backoff_duration = "10s"
  }
  http_target {
    http_method = "POST"
    uri         = "${local.run_url[each.value.svc]}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = local.run_url[each.value.svc]
    }
  }
  depends_on = [google_project_service.apis]
}
