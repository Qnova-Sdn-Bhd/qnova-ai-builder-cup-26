locals {
  common_env = {
    APP_ENV                           = "prototype"
    AI_PROVIDER                       = "vertex"
    AGENT_RUNTIME                     = "vertex_agent_engine"
    AUTH_MODE                         = "identity_platform"
    STORAGE_BACKEND                   = "firestore"
    FILE_STORAGE_BACKEND              = "gcs"
    TASK_BOARD_BACKEND                = "firestore"
    QUEUE_BACKEND                     = "cloud_tasks"
    ALLOW_LIVE_MODEL_CALLS            = "true"
    GOOGLE_CLOUD_PROJECT              = var.project_id
    GOOGLE_CLOUD_LOCATION             = var.region
    GEMINI_MODEL                      = var.gemini_model
    GEMINI_LOCATION                   = var.gemini_location
    VERTEX_AGENT_ENGINE_RESOURCE_NAME = var.agent_engine_resource_name
    FIRESTORE_DATABASE                = google_firestore_database.main.name
    GCS_BUCKET                        = google_storage_bucket.evidence.name
    DOCUMENT_AI_LOCATION              = var.document_ai_location
    DOCUMENT_AI_PROCESSOR_ID          = local.processor_id
    DOCUMENT_AI_PROCESSOR_VERSION     = var.document_ai_processor_version
    CLOUD_TASKS_LOCATION              = var.region
    CLOUD_TASKS_QUEUE                 = google_cloud_tasks_queue.work.name
    WORKER_SERVICE_URL                = local.worker_url
    TASK_INVOKER_SERVICE_ACCOUNT      = google_service_account.runtime["tasks"].email
    SCHEDULER_INVOKER_SERVICE_ACCOUNT = google_service_account.runtime["scheduler"].email
    FIREBASE_PROJECT_ID               = var.project_id
    FIREBASE_AUTH_DOMAIN              = "${var.project_id}.firebaseapp.com"
    CORS_ALLOW_ORIGINS                = local.web_url
    MAX_UPLOAD_BYTES                  = "10485760"
    MAX_SOURCE_PAGES                  = "15"
    MAX_ANALYSIS_SOURCES              = "30"
    MAX_ANALYSIS_CHARS                = "120000"
    MODEL_TIMEOUT_SECONDS             = "60"
    MODEL_MAX_RETRIES                 = "2"
    MODEL_MAX_CALLS                   = "3"
    DOCUMENT_AI_TIMEOUT_SECONDS       = "120"
    TASK_DISPATCH_DEADLINE_SECONDS    = "300"
    WORKER_TIMEOUT_SECONDS            = "300"
    JOB_LEASE_SECONDS                 = "360"
    JOB_MAX_ATTEMPTS                  = "3"
  }
  web_env = {
    NODE_ENV                         = "production"
    NEXT_PUBLIC_API_BASE_URL         = "${local.api_url}/api/v1"
    NEXT_PUBLIC_FIREBASE_API_KEY     = var.firebase_public_api_key
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN = "${var.project_id}.firebaseapp.com"
    NEXT_PUBLIC_FIREBASE_PROJECT_ID  = var.project_id
    NEXT_PUBLIC_FIREBASE_APP_ID      = var.firebase_public_app_id
  }
  services = var.deploy_services ? {
    web    = { image = var.web_image, concurrency = 40, memory = "512Mi", timeout = "60s", env = local.web_env }
    api    = { image = var.api_image, concurrency = 20, memory = "1Gi", timeout = "60s", env = local.common_env }
    worker = { image = var.api_image, concurrency = 1, memory = "2Gi", timeout = "300s", env = local.common_env }
  } : {}
}

resource "google_cloud_run_v2_service" "app" {
  for_each            = local.services
  name                = "hg-${each.key}"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true
  template {
    service_account                  = google_service_account.runtime[each.key].email
    execution_environment            = "EXECUTION_ENVIRONMENT_GEN2"
    timeout                          = each.value.timeout
    max_instance_request_concurrency = each.value.concurrency
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = each.value.image
      args  = each.key == "worker" ? ["worker"] : []
      ports { container_port = 8080 }
      resources {
        limits   = { cpu = "1", memory = each.value.memory }
        cpu_idle = true
      }
      dynamic "env" {
        for_each = each.value.env
        content {
          name  = env.key
          value = env.value
        }
      }
      startup_probe {
        tcp_socket { port = 8080 }
        timeout_seconds   = 1
        period_seconds    = 3
        failure_threshold = 30
      }
    }
  }
  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", each.value.image))
      error_message = "Deploy only real immutable Artifact Registry image digests."
    }
    precondition {
      condition     = can(regex("^projects/(${var.project_id}|${data.google_project.target.number})/locations/${var.region}/reasoningEngines/[A-Za-z0-9_-]+$", var.agent_engine_resource_name))
      error_message = "Supply the actual agent resource in this approved project and region."
    }
    precondition {
      condition     = var.firebase_public_api_key != "" && var.firebase_public_app_id != ""
      error_message = "Complete real Identity Platform public web configuration before service deployment."
    }
  }
  depends_on = [
    google_project_iam_member.database_runtime,
    google_storage_bucket_iam_member.evidence_runtime,
    google_vertex_ai_reasoning_engine_iam_member.worker,
    google_vertex_ai_reasoning_engine_iam_member.agent_sessions,
    google_project_iam_member.api_identity_check,
    google_project_iam_member.worker_ocr,
    google_project_iam_member.service_usage,
    google_cloud_tasks_queue_iam_member.enqueue,
    google_service_account_iam_member.act_as_tasks,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_transport" {
  for_each = var.deploy_services ? toset(["web", "api"]) : toset([])
  name     = google_cloud_run_v2_service.app[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "build_rollout" {
  for_each = local.services
  name     = google_cloud_run_v2_service.app[each.key].name
  location = var.region
  role     = google_project_iam_custom_role.run_rollout.name
  member   = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_cloud_run_v2_service_iam_member" "private_worker" {
  for_each = var.deploy_services ? toset(["tasks", "scheduler"]) : toset([])
  name     = google_cloud_run_v2_service.app["worker"].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_cloud_scheduler_job" "recovery" {
  count            = var.deploy_services ? 1 : 0
  name             = "handoff-guardian-recovery"
  region           = var.region
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "300s"
  retry_config {
    retry_count          = 1
    min_backoff_duration = "10s"
    max_backoff_duration = "60s"
  }
  http_target {
    uri         = "${local.worker_url}/internal/recover"
    http_method = "POST"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oidc_token {
      service_account_email = google_service_account.runtime["scheduler"].email
      audience              = local.worker_url
    }
  }
  depends_on = [google_cloud_run_v2_service_iam_member.private_worker]
}
