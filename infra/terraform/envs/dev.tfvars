project_id     = "REPLACE-convoiq-dev"
env            = "dev"
region         = "us-central1"
image          = "us-central1-docker.pkg.dev/REPLACE-convoiq-dev/convoiq/convoiq:bootstrap"
connector_mode = "simulated"
llm_provider   = "none"
enable_lb      = false
enable_nat     = false
min_instances  = { gateway = 0, redaction = 0, enrichment = 0, orchestrator = 0, action-broker = 0, analytics = 0, monitor = 1 }
max_instances  = { gateway = 3, redaction = 3, enrichment = 3, orchestrator = 3, action-broker = 2, analytics = 2 }
api_principals = {
  # "you@example.com" = "ingest|analyst|approver|admin"
}
alert_emails = []
