resource "google_logging_metric" "job_failures" {
  name        = "handoff_guardian/job_failures"
  description = "Safe structured failed or exhausted job events; no source payloads."
  filter      = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"hg-worker\" AND (jsonPayload.status=\"failed\" OR jsonPayload.event=\"job_failed\")"
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "job_failure" {
  display_name          = "Handoff Guardian failed jobs"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "Worker job failure in five minutes"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/${google_logging_metric.job_failures.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  documentation {
    content   = "Inspect safe job IDs, leases, attempts and outbox state. Pause the queue/recovery only using the reviewed runbook. Provider retries may have incurred costs."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.required]
}

resource "google_monitoring_alert_policy" "queue_backlog" {
  display_name          = "Handoff Guardian queue backlog"
  combiner              = "OR"
  notification_channels = var.notification_channels
  conditions {
    display_name = "More than 20 tasks for five minutes"
    condition_threshold {
      filter          = "resource.type=\"cloud_tasks_queue\" AND resource.labels.queue_id=\"${google_cloud_tasks_queue.work.name}\" AND metric.type=\"cloudtasks.googleapis.com/queue/depth\""
      comparison      = "COMPARISON_GT"
      threshold_value = 20
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
  depends_on = [google_project_service.required]
}

resource "google_billing_budget" "prototype" {
  count           = var.billing_account_id != "" && var.budget_amount > 0 ? 1 : 0
  billing_account = var.billing_account_id
  display_name    = "Handoff Guardian project alert (not a cap)"
  budget_filter {
    projects               = ["projects/${data.google_project.target.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }
  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(floor(var.budget_amount))
    }
  }
  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.9 }
  threshold_rules { threshold_percent = 1.0 }
  all_updates_rule {
    disable_default_iam_recipients   = false
    enable_project_level_recipients  = true
    monitoring_notification_channels = var.notification_channels
  }
  depends_on = [google_project_service.required]
}
