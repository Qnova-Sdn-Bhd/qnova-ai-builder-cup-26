variable "project_id" {
  description = "Existing billing-enabled, explicitly authorized Google Cloud project."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Supply the real authorized Google Cloud project ID."
  }
}
variable "region" {
  description = "Approved common Run, Firestore, GCS, queue, scheduler and Agent Engine region."
  type        = string
}
variable "document_ai_location" {
  description = "Approved Document AI processor location (for example us); availability must be checked."
  type        = string
}
variable "document_ai_processor_version" {
  description = "Verified enabled OCR version ID, never an implicit latest/default alias."
  type        = string
}
variable "existing_document_ai_processor_id" {
  description = "Existing OCR processor ID, or empty to create an OCR_PROCESSOR in the approved location."
  type        = string
  default     = ""
}
variable "gemini_model" {
  description = "Actual approved available Vertex Gemini model ID."
  type        = string
}
variable "gemini_location" {
  description = "Model inference location, separate from Agent Runtime compute. Current Flash supports us multi-region."
  type        = string
  default     = "us"
  validation {
    condition     = contains(["us", "eu", "global"], var.gemini_location)
    error_message = "Select an explicitly supported Gemini inference location."
  }
}
variable "agent_engine_resource_name" {
  description = "Actual deployed Agent Engine reasoningEngines resource; supplied after agent deployment."
  type        = string
  default     = ""
}
variable "deploy_services" {
  description = "False for resource bootstrap, true only after real agent and immutable images exist."
  type        = bool
  default     = false
}
variable "api_image" {
  description = "API/worker Artifact Registry image at @sha256 digest."
  type        = string
  default     = ""
}
variable "web_image" {
  description = "Web Artifact Registry image at @sha256 digest."
  type        = string
  default     = ""
}
variable "firebase_public_api_key" {
  description = "Public Identity Platform web API key, restricted to approved web origins and identity APIs."
  type        = string
  default     = ""
}
variable "firebase_public_app_id" {
  description = "Public registered Firebase web app ID, resolved during identity setup."
  type        = string
  default     = ""
}
variable "additional_authorized_domains" {
  description = "Explicit reviewed identity callback hostnames (no scheme, path, or wildcards)."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for d in var.additional_authorized_domains : can(regex("^[a-zA-Z0-9.-]+$", d)) && !strcontains(d, "*")])
    error_message = "Authorized domains must be exact hostnames."
  }
}
variable "operator_members" {
  description = "Explicit IAM members authorized for trusted bootstrap (user:email or group:email). No default access."
  type        = set(string)
  default     = []
}
variable "notification_channels" {
  description = "Existing verified Cloud Monitoring notification-channel resource names."
  type        = list(string)
  default     = []
}
variable "billing_account_id" {
  description = "Optional authorized billing account ID to configure the project-filtered budget alert."
  type        = string
  default     = ""
}
variable "budget_amount" {
  description = "Explicit monthly alert amount in billing-account currency; alerts are not spending caps."
  type        = number
  default     = 0
}
variable "budget_currency" {
  description = "Billing-account currency; required if budget is configured."
  type        = string
  default     = "USD"
}
variable "private_secret_ids" {
  description = "Only actually needed private secret containers; versions added separately, never through Terraform."
  type        = set(string)
  default     = []
}
