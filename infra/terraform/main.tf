locals {
  apis = toset([
    "run.googleapis.com", "identitytoolkit.googleapis.com", "firestore.googleapis.com",
    "storage.googleapis.com", "documentai.googleapis.com", "aiplatform.googleapis.com",
    "cloudtasks.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com",
    "logging.googleapis.com", "monitoring.googleapis.com", "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com", "iam.googleapis.com", "iamcredentials.googleapis.com",
    "serviceusage.googleapis.com", "firebaserules.googleapis.com", "cloudbilling.googleapis.com",
    "billingbudgets.googleapis.com", "firebase.googleapis.com", "apikeys.googleapis.com"
  ])
  accounts           = toset(["web", "api", "worker", "agent", "tasks", "scheduler", "build"])
  worker_url         = "https://hg-worker-${data.google_project.target.number}.${var.region}.run.app"
  api_url            = "https://hg-api-${data.google_project.target.number}.${var.region}.run.app"
  web_url            = "https://hg-web-${data.google_project.target.number}.${var.region}.run.app"
  processor_id       = var.existing_document_ai_processor_id != "" ? var.existing_document_ai_processor_id : basename(google_document_ai_processor.ocr[0].id)
  processor_resource = "projects/${var.project_id}/locations/${var.document_ai_location}/processors/${local.processor_id}"
}

data "google_project" "target" { project_id = var.project_id }

resource "google_project_service" "required" {
  for_each           = local.apis
  service            = each.key
  disable_on_destroy = false
}

resource "google_service_account" "runtime" {
  for_each     = local.accounts
  account_id   = "hg-${each.key}"
  display_name = "Handoff Guardian ${each.key}"
  depends_on   = [google_project_service.required]
}

resource "google_storage_bucket" "evidence" {
  name                        = "${var.project_id}-hg-evidence"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  depends_on = [google_project_service.required]
  lifecycle { prevent_destroy = true }
}

resource "google_storage_bucket" "agent_staging" {
  name                        = "${var.project_id}-hg-agent-staging"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "build_source" {
  name                        = "${var.project_id}-hg-build-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning { enabled = true }
  depends_on = [google_project_service.required]
}

resource "google_firestore_database" "main" {
  name                    = "(default)"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  concurrency_mode        = "PESSIMISTIC"
  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON"
  depends_on              = [google_project_service.required]
}

resource "google_firebaserules_ruleset" "deny_clients" {
  source {
    files {
      name    = "firestore.rules"
      content = file("${path.module}/firestore.rules")
    }
  }
  depends_on = [google_firestore_database.main]
}

resource "google_firebaserules_release" "firestore" {
  name         = "cloud.firestore"
  ruleset_name = google_firebaserules_ruleset.deny_clients.name
}

resource "google_document_ai_processor" "ocr" {
  count        = var.existing_document_ai_processor_id == "" ? 1 : 0
  location     = var.document_ai_location
  display_name = "Handoff Guardian OCR"
  type         = "OCR_PROCESSOR"
  depends_on   = [google_project_service.required]
}

resource "google_identity_platform_config" "main" {
  authorized_domains = distinct(concat([
    "${var.project_id}.firebaseapp.com",
    trimprefix(local.web_url, "https://")
  ], var.additional_authorized_domains))
  sign_in {
    allow_duplicate_emails = false
    anonymous { enabled = false }
    email {
      enabled           = true
      password_required = true
    }
    phone_number { enabled = false }
  }
  # Email is the username. Identity Platform owns password verification and
  # credentials; no password or OAuth client secret enters Terraform state.
  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "handoff-guardian"
  format        = "DOCKER"
  description   = "Immutable Handoff Guardian build images"
  docker_config { immutable_tags = true }
  depends_on = [google_project_service.required]
}

resource "google_cloud_tasks_queue" "work" {
  name     = "handoff-guardian-work"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 2
    max_dispatches_per_second = 2
  }
  retry_config {
    max_attempts       = 5
    max_retry_duration = "1800s"
    min_backoff        = "10s"
    max_backoff        = "300s"
    max_doublings      = 4
  }
  stackdriver_logging_config { sampling_ratio = 1 }
  # HTTP retry count differs from the three reserved durable external attempts.
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "private" {
  for_each  = var.private_secret_ids
  secret_id = each.key
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}
