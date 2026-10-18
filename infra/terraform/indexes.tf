# Recovery can query pending outbox and expired leases without unbounded scans.
resource "google_firestore_field" "membership_lookup" {
  database   = google_firestore_database.main.name
  collection = "memberships"
  field      = "user_id"
  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION"
    }
  }
}

resource "google_firestore_index" "job_recovery" {
  database   = google_firestore_database.main.name
  collection = "jobs"
  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "lease_until"
    order      = "ASCENDING"
  }
}

resource "google_firestore_index" "outbox_recovery" {
  database   = google_firestore_database.main.name
  collection = "outbox"
  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
  fields {
    field_path = "created_at"
    order      = "ASCENDING"
  }
}

locals {
  unindexed_fields = {
    "sources.page_spans"       = ["sources", "page_spans"]
    "commitments.evidence"     = ["commitments", "evidence"]
    "findings.evidence"        = ["findings", "evidence"]
    "findings.explanation"     = ["findings", "explanation"]
    "action_proposals.payload" = ["action_proposals", "payload"]
    "tasks.description"        = ["tasks", "description"]
    "analyses.warnings"        = ["analyses", "warnings"]
  }
}

resource "google_firestore_field" "unindexed" {
  for_each   = local.unindexed_fields
  database   = google_firestore_database.main.name
  collection = each.value[0]
  field      = each.value[1]
  index_config {}
}
