output "project_id" { value = var.project_id }
output "project_number" { value = data.google_project.target.number }
output "region" { value = var.region }
output "web_url" { value = var.deploy_services ? local.web_url : null }
output "api_url" { value = var.deploy_services ? local.api_url : null }
output "worker_url" { value = var.deploy_services ? local.worker_url : null }
output "expected_service_origins" {
  description = "Documented deterministic origins; verify against deployed Run URLs. Not evidence of deployment."
  value       = { web = local.web_url, api = local.api_url, worker = local.worker_url }
}
output "evidence_bucket" { value = google_storage_bucket.evidence.name }
output "agent_staging_bucket" { value = google_storage_bucket.agent_staging.name }
output "build_source_bucket" { value = google_storage_bucket.build_source.name }
output "processor_resource" { value = local.processor_resource }
output "processor_version_resource" { value = "${local.processor_resource}/processorVersions/${var.document_ai_processor_version}" }
output "agent_resource" { value = var.agent_engine_resource_name }
output "queue" { value = google_cloud_tasks_queue.work.id }
output "image_repository" { value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}" }
output "service_accounts" { value = { for key, account in google_service_account.runtime : key => account.email } }
output "public_web_configuration" { value = local.web_env }
output "budget_alert_configured" { value = length(google_billing_budget.prototype) == 1 }
