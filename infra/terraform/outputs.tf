output "gateway_url" {
  value = local.run_url["gateway"]
}
output "service_urls" {
  value = local.run_url
}
output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}"
}
output "lb_ip" {
  value = var.enable_lb ? google_compute_global_address.gw[0].address : null
}
output "service_accounts" {
  value = { for k, v in google_service_account.svc : k => v.email }
}
output "dlq_subscription" {
  value = google_pubsub_subscription.dlq_pull.name
}
output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}
