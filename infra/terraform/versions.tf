terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.8" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Remote state: create the bucket once (versioned, private, CMEK recommended) and init with
  #   terraform init -backend-config="bucket=<state-bucket>" -backend-config="prefix=convoiq/<env>"
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
