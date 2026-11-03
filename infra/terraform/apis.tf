locals {
  apis = [
    "run.googleapis.com", "pubsub.googleapis.com", "bigquery.googleapis.com", "firestore.googleapis.com",
    "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "cloudscheduler.googleapis.com",
    "monitoring.googleapis.com", "logging.googleapis.com", "aiplatform.googleapis.com", "compute.googleapis.com",
    "iam.googleapis.com", "iamcredentials.googleapis.com", "cloudkms.googleapis.com", "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

data "google_project" "this" {
  project_id = var.project_id
  depends_on = [google_project_service.apis]
}
