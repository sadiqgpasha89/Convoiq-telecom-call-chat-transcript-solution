variable "project_id" {
  type        = string
  description = "GCP project that hosts this environment (one project per environment)."
}
variable "region" {
  type    = string
  default = "us-central1"
}
variable "env" {
  type        = string
  description = "dev | staging | prod. Used as resource-name prefix and as CONVOIQ_ENV (prod enforces strict runtime validation)."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be dev, staging or prod."
  }
}
variable "image" {
  type        = string
  description = "Fully-qualified image (digest preferred): REGION-docker.pkg.dev/PROJECT/convoiq/convoiq@sha256:..."
}
variable "bq_location" {
  type        = string
  default     = "US"
  description = "BigQuery dataset location. Pick the region/multi-region that satisfies your data-residency rules."
}
variable "firestore_location" {
  type    = string
  default = "nam5"
}
variable "retention_days" {
  type        = number
  default     = 395
  description = "BigQuery partition expiry for fact tables (privacy: pseudonymous analytics are not kept forever)."
}
variable "cmek_key_id" {
  type        = string
  default     = ""
  description = "Optional Cloud KMS key (projects/../cryptoKeys/..) for BigQuery, Pub/Sub raw topic and the artifacts bucket."
}

# --- authentication / authorisation -------------------------------------------------------------------------------
variable "api_principals" {
  type        = map(string)
  default     = {}
  description = <<-EOT
    External callers of the gateway: email => "role|role". Roles: ingest, analyst, approver, admin.
    e.g. { "partner-ingest@acme.iam.gserviceaccount.com" = "ingest", "alice@corp.com" = "analyst|approver" }
    Each also receives roles/run.invoker on the gateway (Cloud Run IAM is the first gate, the app RBAC the second).
  EOT
}
variable "gateway_public" {
  type        = bool
  default     = false
  description = "Grant allUsers run.invoker on the gateway (needed only behind browsers/partners without Google identities; app auth still applies)."
}

# --- sizing -----------------------------------------------------------------------------------------------------------
variable "min_instances" {
  type = map(number)
  default = {
    gateway = 1, redaction = 1, enrichment = 1, orchestrator = 1, action-broker = 1, analytics = 0, monitor = 1
  }
  description = "Warm instances per service (monitor is pinned to exactly 1 regardless: its detector state is in memory)."
}
variable "max_instances" {
  type = map(number)
  default = {
    gateway = 20, redaction = 20, enrichment = 30, orchestrator = 30, action-broker = 10, analytics = 10
  }
}

# --- features -------------------------------------------------------------------------------------------------------------
variable "llm_provider" {
  type        = string
  default     = "none"
  description = "none | vertex | anthropic. 'none' keeps the pipeline fully deterministic (recommended baseline)."
}
variable "llm_model" {
  type    = string
  default = "gemini-2.5-flash"
}
variable "llm_daily_budget_usd" {
  type    = number
  default = 200
}
variable "connector_mode" {
  type        = string
  default     = "simulated"
  description = "simulated (deploys telecom-sim) | dryrun | real. 'real' needs connector secrets populated first (see docs/INTEGRATIONS.md)."
  validation {
    condition     = contains(["simulated", "dryrun", "real"], var.connector_mode)
    error_message = "connector_mode must be simulated, dryrun or real."
  }
}
variable "salesforce_url" {
  type    = string
  default = ""
}
variable "servicenow_url" {
  type    = string
  default = ""
}
variable "billing_url" {
  type    = string
  default = ""
}
variable "enable_lb" {
  type        = bool
  default     = false
  description = "Global external HTTPS LB + Cloud Armor in front of the gateway (requires var.domain)."
}
variable "domain" {
  type    = string
  default = ""
}
variable "enable_nat" {
  type        = bool
  default     = true
  description = "Cloud NAT so VPC-egress services can reach non-Google APIs (Anthropic, real CRM/billing endpoints)."
}
variable "alert_emails" {
  type    = list(string)
  default = []
}
