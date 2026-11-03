project_id     = "REPLACE-convoiq-prod"
env            = "prod"
region         = "us-central1"
image          = "us-central1-docker.pkg.dev/REPLACE-convoiq-prod/convoiq/convoiq:bootstrap"
bq_location    = "US"
connector_mode = "real" # populate the three connector secrets first (docs/INTEGRATIONS.md)
llm_provider   = "none" # flip to "vertex" only after the privacy review in docs/SECURITY.md
enable_lb      = true
domain         = "convoiq.example.com"
retention_days = 395
min_instances  = { gateway = 2, redaction = 2, enrichment = 2, orchestrator = 2, action-broker = 1, analytics = 1, monitor = 1 }
max_instances  = { gateway = 50, redaction = 50, enrichment = 100, orchestrator = 100, action-broker = 20, analytics = 20 }
api_principals = {
  # "partner-ingest@partner.iam.gserviceaccount.com" = "ingest"
  # "analyst@yourco.com"                             = "analyst"
  # "approver@yourco.com"                            = "approver"
}
alert_emails = ["oncall@example.com"]
