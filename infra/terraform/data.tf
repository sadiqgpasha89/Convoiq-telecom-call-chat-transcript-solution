# ---------------------------------------------------------------------------------------------------------------------
# Registry, warehouse, state store, artifacts, secrets
# ---------------------------------------------------------------------------------------------------------------------
resource "google_artifact_registry_repository" "repo" {
  repository_id = "convoiq"
  location      = var.region
  format        = "DOCKER"
  description   = "ConvoIQ service images"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 20 }
  }
  depends_on = [google_project_service.apis]
}

resource "google_bigquery_dataset" "lake" {
  dataset_id                 = "convoiq_lakehouse"
  location                   = var.bq_location
  description                = "ConvoIQ analytics plane: pseudonymous facts only (no raw transcripts, no direct identifiers)."
  delete_contents_on_destroy = false
  dynamic "default_encryption_configuration" {
    for_each = var.cmek_key_id == "" ? [] : [1]
    content { kms_key_name = var.cmek_key_id }
  }
  labels     = { env = var.env, app = "convoiq" }
  depends_on = [google_project_service.apis]
}

# Table definitions are GENERATED from convoiq/platform/schemas.py (scripts/gen_infra.py) - the code is the source of truth.
resource "google_bigquery_table" "t" {
  for_each            = local.bq_tables
  dataset_id          = google_bigquery_dataset.lake.dataset_id
  table_id            = each.key
  schema              = jsonencode(each.value.schema)
  deletion_protection = var.env == "prod"
  time_partitioning {
    type          = "DAY"
    field         = each.value.partition_field
    expiration_ms = var.retention_days * 24 * 3600 * 1000
  }
  clustering = each.value.clustering
  labels     = { env = var.env }
}

resource "google_firestore_database" "main" {
  name                              = "(default)"
  location_id                       = var.firestore_location
  type                              = "FIRESTORE_NATIVE"
  point_in_time_recovery_enablement = var.env == "prod" ? "POINT_IN_TIME_RECOVERY_ENABLED" : "POINT_IN_TIME_RECOVERY_DISABLED"
  delete_protection_state           = var.env == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  depends_on                        = [google_project_service.apis]
}

# TTL policies on the `_expires_at` field written by FirestoreStore for collections that carry a ttl
locals {
  ttl_collections = concat(
    ["convoiq_ingest", "convoiq_status", "convoiq_analysis", "convoiq_agent_ids", "convoiq_open_tickets", "convoiq_llm_budget"],
    [for s in local.services : "convoiq_idem_${s}"]
  )
}
resource "google_firestore_field" "ttl" {
  for_each   = toset(local.ttl_collections)
  project    = var.project_id
  database   = google_firestore_database.main.name
  collection = each.value
  field      = "_expires_at"
  ttl_config {}
  index_config {}
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-convoiq-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  dynamic "encryption" {
    for_each = var.cmek_key_id == "" ? [] : [1]
    content { default_kms_key_name = var.cmek_key_id }
  }
  lifecycle_rule {
    condition { age = 365 }
    action { type = "Delete" }
  }
  labels     = { env = var.env }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------------------------------------------------
# Secrets. Values are generated once; rotate by adding a new secret VERSION outside Terraform (ignore_changes on secret_data).
# The Terraform state therefore contains the initial values - keep the state bucket private and encrypted.
# ---------------------------------------------------------------------------------------------------------------------
resource "random_password" "pii_hmac" {
  length  = 64
  special = false
}
resource "random_password" "attestation" {
  length  = 64
  special = false
}
resource "random_id" "vault" {
  byte_length = 32
}

locals {
  secret_values = {
    "pii-hmac-key"    = random_password.pii_hmac.result
    "attestation-key" = random_password.attestation.result
    "vault-key"       = "${random_id.vault.b64_url}=" # Fernet key = 32 bytes, url-safe base64 WITH padding
  }
}
resource "google_secret_manager_secret" "s" {
  for_each  = local.secret_values
  secret_id = "${local.prefix}-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "v" {
  for_each    = local.secret_values
  secret      = google_secret_manager_secret.s[each.key].id
  secret_data = each.value
  lifecycle { ignore_changes = [secret_data] }
}

# every service needs the tokenisation/attestation keys (prod runtime validation); only redaction holds the vault key
locals {
  secret_access = merge(
    { for s in local.services : "${s}|pii-hmac-key" => { svc = s, secret = "pii-hmac-key" } },
    { for s in local.services : "${s}|attestation-key" => { svc = s, secret = "attestation-key" } },
    { "redaction|vault-key" = { svc = "redaction", secret = "vault-key" } },
  )
}
resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = local.secret_access
  secret_id = google_secret_manager_secret.s[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc[each.value.svc].email}"
}

# Connector credentials (real mode): containers only - populate with `gcloud secrets versions add` BEFORE switching connector_mode=real.
resource "google_secret_manager_secret" "connector" {
  for_each  = var.connector_mode == "simulated" ? toset([]) : toset(["salesforce-token", "servicenow-token", "billing-token"])
  secret_id = "${local.prefix}-${each.key}"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret_iam_member" "connector_access" {
  for_each  = google_secret_manager_secret.connector
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.svc["action-broker"].email}"
}
