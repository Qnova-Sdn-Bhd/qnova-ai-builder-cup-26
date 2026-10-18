# Every IAM resource is additive; preserve Google-managed service-agent bindings.
resource "google_project_iam_custom_role" "immutable_objects" {
  role_id     = "hgImmutableObjectAccess"
  title       = "Handoff Guardian immutable evidence access"
  permissions = ["storage.objects.create", "storage.objects.get"]
}

resource "google_storage_bucket_iam_member" "evidence_runtime" {
  for_each = toset(["api", "worker"])
  bucket   = google_storage_bucket.evidence.name
  role     = google_project_iam_custom_role.immutable_objects.name
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_project_iam_member" "database_runtime" {
  for_each = toset(["api", "worker"])
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
  condition {
    title      = "handoff-guardian-database"
    expression = "resource.name == 'projects/${var.project_id}/databases/(default)' || resource.name == 'projects/${data.google_project.target.number}/databases/(default)'"
  }
}

resource "google_cloud_tasks_queue_iam_member" "enqueue" {
  for_each = toset(["api", "worker"])
  name     = google_cloud_tasks_queue.work.name
  location = var.region
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_service_account_iam_member" "act_as_tasks" {
  for_each           = toset(["api", "worker"])
  service_account_id = google_service_account.runtime["tasks"].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_project_iam_custom_role" "online_ocr" {
  role_id     = "hgPinnedOcr"
  title       = "Handoff Guardian online OCR only"
  permissions = ["documentai.processorVersions.processOnline"]
}

# Document AI is absent from the documented resource.name condition support table
# and this provider has no processor IAM resource. Use the single online-version
# permission at project scope; the worker pins the processor/version in code.
resource "google_project_iam_member" "worker_ocr" {
  project = var.project_id
  role    = google_project_iam_custom_role.online_ocr.name
  member  = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

resource "google_project_iam_custom_role" "query_agent" {
  role_id     = "hgQueryAgent"
  title       = "Handoff Guardian configured agent invocation"
  permissions = ["aiplatform.reasoningEngines.query", "aiplatform.reasoningEngines.get"]
}

resource "google_project_iam_custom_role" "verify_identity" {
  role_id     = "hgIdentityRevocationCheck"
  title       = "Handoff Guardian user revocation check"
  permissions = ["firebaseauth.users.get"]
}

resource "google_project_iam_member" "api_identity_check" {
  project = var.project_id
  role    = google_project_iam_custom_role.verify_identity.name
  member  = "serviceAccount:${google_service_account.runtime["api"].email}"
}

resource "google_vertex_ai_reasoning_engine_iam_member" "worker" {
  count            = var.agent_engine_resource_name == "" ? 0 : 1
  reasoning_engine = var.agent_engine_resource_name
  region           = var.region
  role             = google_project_iam_custom_role.query_agent.name
  member           = "serviceAccount:${google_service_account.runtime["worker"].email}"
}

resource "google_project_iam_custom_role" "agent_sessions" {
  role_id = "hgManagedAgentSessions"
  title   = "Handoff Guardian managed agent session operations"
  # ADK VertexAiSessionService creates/gets a session, lists its events, and
  # appends user/agent events during query. Our workflow never lists sessions,
  # updates/deletes sessions, accesses Memory Bank, or launches sandboxes.
  permissions = [
    "aiplatform.sessions.create",
    "aiplatform.sessions.get",
    "aiplatform.sessionEvents.append",
    "aiplatform.sessionEvents.list",
  ]
}

resource "google_vertex_ai_reasoning_engine_iam_member" "agent_sessions" {
  count            = var.agent_engine_resource_name == "" ? 0 : 1
  reasoning_engine = var.agent_engine_resource_name
  region           = var.region
  role             = google_project_iam_custom_role.agent_sessions.name
  member           = "serviceAccount:${google_service_account.runtime["agent"].email}"
}

resource "google_project_iam_custom_role" "model_inference" {
  role_id     = "hgModelInference"
  title       = "Handoff Guardian Vertex prediction only"
  permissions = ["aiplatform.endpoints.predict"]
}

resource "google_project_iam_member" "agent_model" {
  project = var.project_id
  role    = google_project_iam_custom_role.model_inference.name
  member  = "serviceAccount:${google_service_account.runtime["agent"].email}"
}

resource "google_project_iam_member" "service_usage" {
  for_each = toset(["api", "worker", "agent", "build"])
  project  = var.project_id
  role     = "roles/serviceusage.serviceUsageConsumer"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_project_iam_member" "runtime_logs" {
  for_each = local.accounts
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = "serviceAccount:${google_service_account.runtime[each.key].email}"
}

resource "google_artifact_registry_repository_iam_member" "build_push" {
  location   = var.region
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_project_iam_custom_role" "run_rollout" {
  role_id     = "hgRunImageRollout"
  title       = "Handoff Guardian existing service rollout"
  permissions = ["run.services.get", "run.services.update"]
}

resource "google_project_iam_custom_role" "run_operations" {
  role_id     = "hgRunOperationRead"
  title       = "Handoff Guardian rollout operation status"
  permissions = ["run.operations.get"]
}

resource "google_project_iam_member" "build_deploy_run_operations" {
  project = var.project_id
  role    = google_project_iam_custom_role.run_operations.name
  member  = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_service_account_iam_member" "build_runtime" {
  for_each           = toset(["web", "api", "worker", "agent"])
  service_account_id = google_service_account.runtime[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_storage_bucket_iam_member" "agent_staging_build" {
  bucket = google_storage_bucket.agent_staging.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_project_iam_custom_role" "staging_bucket_read" {
  role_id     = "hgStagingBucketRead"
  title       = "Handoff Guardian staging bucket metadata read"
  permissions = ["storage.buckets.get"]
}

resource "google_storage_bucket_iam_member" "agent_staging_build_metadata" {
  bucket = google_storage_bucket.agent_staging.name
  role   = google_project_iam_custom_role.staging_bucket_read.name
  member = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_storage_bucket_iam_member" "build_source_read" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_storage_bucket_iam_member" "build_source_operator" {
  for_each = var.operator_members
  bucket   = google_storage_bucket.build_source.name
  role     = "roles/storage.objectAdmin"
  member   = each.key
}

resource "google_service_account_iam_member" "agent_runtime_token" {
  service_account_id = google_service_account.runtime["agent"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.target.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  depends_on         = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "agent_staging_service_agent" {
  bucket     = google_storage_bucket.agent_staging.name
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:service-${data.google_project.target.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  depends_on = [google_project_service.required]
}

resource "google_project_iam_custom_role" "agent_deploy" {
  role_id = "hgAgentDeployer"
  title   = "Handoff Guardian agent deployment"
  permissions = [
    "aiplatform.reasoningEngines.create", "aiplatform.reasoningEngines.get",
    "aiplatform.reasoningEngines.update", "aiplatform.reasoningEngines.list",
  ]
}

resource "google_project_iam_member" "agent_deploy" {
  project = var.project_id
  role    = google_project_iam_custom_role.agent_deploy.name
  member  = "serviceAccount:${google_service_account.runtime["build"].email}"
}

resource "google_project_iam_member" "operator_database" {
  for_each = var.operator_members
  project  = var.project_id
  role     = "roles/datastore.user"
  member   = each.key
  condition {
    title      = "handoff-guardian-bootstrap-database"
    expression = "resource.name == 'projects/${var.project_id}/databases/(default)' || resource.name == 'projects/${data.google_project.target.number}/databases/(default)'"
  }
}

resource "google_project_iam_member" "operator_identity" {
  for_each = var.operator_members
  project  = var.project_id
  role     = "roles/firebaseauth.viewer"
  member   = each.key
}

resource "google_project_iam_custom_role" "reviewer_bootstrap_token" {
  role_id     = "hgReviewerBootstrapToken"
  title       = "Handoff Guardian reviewer bootstrap access token"
  permissions = ["iam.serviceAccounts.getAccessToken"]
}

# Firebase Admin rejects the standard gcloud user OAuth client. Trusted operators
# can instead mint a short-lived access token for the existing API identity.
# Bind only on this service account; do not grant project-wide impersonation.
resource "google_service_account_iam_member" "operator_bootstrap_token" {
  for_each           = var.operator_members
  service_account_id = google_service_account.runtime["api"].name
  role               = google_project_iam_custom_role.reviewer_bootstrap_token.name
  member             = each.key
}
