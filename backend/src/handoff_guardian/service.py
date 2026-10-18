"""Domain orchestration. Transaction callbacks contain only Firestore reads/writes."""

import copy
import json
import re
import uuid
from zoneinfo import ZoneInfo

from .contracts import canonical, digest, public, validate
from .errors import AppError, require
from .evidence import active_baseline, business_now, check_span, now, timestamp
from .ports import Actor


def identifier(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def path(project_id, collection=None, entity_id=None):
    result = f"projects/{project_id}"
    if collection:
        result += f"/{collection}"
    if entity_id:
        result += f"/{entity_id}"
    return result


class Guardian:
    def __init__(self, settings, repository, store, dispatcher):
        self.settings, self.repo, self.store, self.dispatcher = settings, repository, store, dispatcher

    def actor_for_uid(self, uid):
        binding_id = digest(f"{self.settings.firebase_project}:{uid}".encode())
        binding = self.repo.get(f"identity_bindings/{binding_id}")
        require(
            binding is not None
            and binding.get("enabled") is True
            and binding.get("uid") == uid
            and binding.get("identity_project_id") == self.settings.firebase_project,
            "IDENTITY_NOT_PROVISIONED",
            "This signed-in identity has not been provisioned.",
            403,
        )
        require(
            self.repo.get(f"users/{binding['user_id']}") is not None,
            "IDENTITY_NOT_PROVISIONED",
            "The provisioned user is unavailable.",
            403,
        )
        return Actor(binding["user_id"], binding_id, binding["version"])

    @staticmethod
    def authorize(unit, actor, project_id, reviewer=False):
        binding = unit.get(f"identity_bindings/{actor.binding_id}")
        require(
            binding is not None
            and binding.get("enabled") is True
            and binding.get("user_id") == actor.user_id
            and binding.get("version") == actor.binding_version,
            "IDENTITY_NOT_PROVISIONED",
            "The identity binding has changed or been disabled.",
            403,
        )
        project = unit.get(path(project_id))
        member = unit.get(path(project_id, "memberships", actor.user_id))
        require(project is not None and member is not None, "NOT_FOUND", "Project not found.", 404)
        require(
            not reviewer or member["role"] == "reviewer",
            "REVIEWER_REQUIRED",
            "A project reviewer must perform this operation.",
            403,
        )
        return project, member

    @staticmethod
    def entity(unit, project_id, collection, entity_id):
        validate("Id", entity_id)
        value = unit.get(path(project_id, collection, entity_id))
        require(
            value is not None and value.get("project_id") == project_id,
            "NOT_FOUND",
            "Project object not found.",
            404,
        )
        return value

    @staticmethod
    def revisions(project, evidence=None, board=None):
        require(
            evidence is None or project["evidence_revision"] == evidence,
            "STALE_EVIDENCE",
            "Evidence changed; refresh and review the current packet.",
        )
        require(
            board is None or project["board_revision"] == board,
            "STALE_TARGET",
            "The board changed; refresh and review the current target.",
        )

    @staticmethod
    def audit(unit, project, actor_id, event, target, summary, payload_hash=None):
        record = {
            "id": identifier("aud"),
            "project_id": project["id"],
            "actor_id": actor_id,
            "occurred_at": now(),
            "event_type": event,
            "target_id": target,
            "summary": summary,
            "evidence_revision": project["evidence_revision"],
            "board_revision": project["board_revision"],
            "payload_hash": payload_hash,
        }
        unit.create(path(project["id"], "audit_events", record["id"]), record)

    @staticmethod
    def receipt_path(project_id, actor, operation, key):
        require(
            isinstance(key, str) and 8 <= len(key) <= 200,
            "INVALID_REQUEST",
            "An Idempotency-Key of 8 to 200 characters is required.",
            422,
        )
        return path(project_id, "receipts", digest([actor.user_id, operation, key]))

    @staticmethod
    def replay(unit, receipt_path, request_hash):
        record = unit.get(receipt_path)
        require(
            not record or record["request_hash"] == request_hash,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was used for a different request.",
        )
        return record

    def list_projects(self, actor):
        values = []
        for project_id in self.repo.accessible_projects(actor.user_id):
            project, _ = self.authorize(self.repo, actor, project_id)
            values.append(self.project_view(project))
        return {"projects": values}

    def create_project(self, actor, body, key):
        validate("ProjectCreateRequest", body)
        name = body["name"].strip()
        require(bool(name), "INVALID_REQUEST", "Enter a project name.", 422)
        try:
            ZoneInfo(body["timezone"])
        except (ValueError, KeyError) as exc:
            raise AppError("INVALID_REQUEST", "Choose a valid IANA timezone.", 422) from exc
        sponsor = body["reviewer_project_id"]
        receipt = self.receipt_path(sponsor, actor, "create_project", key)
        request_hash = digest(body)
        project = {
            "id": identifier("prj"),
            "name": name,
            "timezone": body["timezone"],
            "business_clock": {"mode": "real", "as_of": now()},
            "evidence_revision": 0,
            "board_revision": 0,
        }
        validate("Project", project)

        def create(unit):
            self.authorize(unit, actor, sponsor, True)
            previous = self.replay(unit, receipt, request_hash)
            if previous:
                existing, _ = self.authorize(unit, actor, previous["project_id"], True)
                return existing
            unit.create(path(project["id"]), project)
            unit.create(
                path(project["id"], "memberships", actor.user_id),
                {"user_id": actor.user_id, "role": "reviewer"},
            )
            self.audit(
                unit,
                project,
                actor.user_id,
                "project_created",
                project["id"],
                "Reviewer created an empty project.",
            )
            self.audit(
                unit,
                project,
                actor.user_id,
                "membership_granted",
                actor.user_id,
                "Project creator granted reviewer membership.",
            )
            unit.create(receipt, {"request_hash": request_hash, "project_id": project["id"]})
            return project

        return self.project_view(self.repo.transact(create))

    @staticmethod
    def project_view(project):
        value = public(copy.deepcopy(project))
        value["business_clock"]["as_of"] = business_now(project)
        return value

    def workspace(self, actor, project_id):
        project, member = self.authorize(self.repo, actor, project_id)
        memberships = self.repo.list(path(project_id, "memberships"), 101)
        users = [self.repo.get(f"users/{item['user_id']}") for item in memberships]
        result = {
            "project": self.project_view(project),
            "actor_role": member["role"],
            "users": [public(user) for user in users if user],
            "memberships": public(memberships),
        }
        for collection, limit in [
            ("sources", 101),
            ("commitments", 501),
            ("action_proposals", 501),
            ("tasks", 501),
        ]:
            result[collection] = public(self.repo.list(path(project_id, collection), limit))
        visible_findings = self.repo.where(path(project_id, "findings"), "status", "open", 501)
        current_findings = self.repo.where(
            path(project_id, "findings"), "analysis_run_id", project.get("_current_analysis_run_id"), 501
        )
        result["findings"] = public(
            list({item["id"]: item for item in [*visible_findings, *current_findings]}.values())
        )
        runs = self.repo.recent(path(project_id, "analyses"), "created_at", 20)
        result["analysis_runs"] = [self.run_view(project_id, item) for item in runs]
        result["audit_events"] = public(
            self.repo.recent(path(project_id, "audit_events"), "occurred_at", 100)
        )
        # A stored lifecycle label is never used in place of mandatory freshness checks.
        for action in result["action_proposals"]:
            if action["status"] in {"proposed", "approved"}:
                try:
                    self.check_action_current(self.repo, project, action)
                except AppError:
                    action["status"] = "stale"
        return result

    def source_view(self, source):
        result = public(source)
        result["canonical_text"] = (
            self.store.read(source["_canonical"]).decode() if source.get("_canonical") else None
        )
        result["page_spans"] = source.get("_page_spans", [])
        return result

    def source(self, actor, project_id, source_id):
        self.authorize(self.repo, actor, project_id)
        return self.source_view(self.entity(self.repo, project_id, "sources", source_id))

    def original(self, actor, project_id, source_id):
        self.authorize(self.repo, actor, project_id)
        source = self.entity(self.repo, project_id, "sources", source_id)
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", source.get("_filename", "evidence"))[:160] or "evidence"
        return self.store.read(source["_original"]), source["mime_type"], filename

    @staticmethod
    def new_job(unit, project_id, target_id, kind, actor, snapshot=None):
        job_id = identifier("job")
        job = {
            "id": job_id,
            "project_id": project_id,
            "target_id": target_id,
            "kind": kind,
            "actor_id": actor.user_id,
            "binding_id": actor.binding_id,
            "binding_version": actor.binding_version,
            "status": "pending",
            "attempt_count": 0,
            "fencing_token": 0,
            "dispatch_generation": 1,
            "lease_until": None,
            "created_at": now(),
            "pending_since": now(),
            "snapshot": snapshot,
        }
        unit.create(f"jobs/{job_id}", job)
        unit.create(f"outbox/{job_id}", {"job_id": job_id, "generation": 1, "status": "pending"})
        return job_id

    def dispatch(self, job_id):
        """Failure leaves a durable pending outbox for Scheduler; never loses the job."""
        record = self.repo.get(f"outbox/{job_id}")
        if not record or record["status"] != "pending":
            return
        try:
            self.dispatcher.enqueue(job_id, record["generation"])
        except Exception:
            return

        def mark(unit):
            latest = unit.get(f"outbox/{job_id}")
            if latest and latest["generation"] == record["generation"]:
                latest["status"] = "dispatched"
                latest["dispatched_at"] = now()
                unit.set(f"outbox/{job_id}", latest)

        self.repo.transact(mark)

    def upload(self, actor, project_id, metadata, raw, filename, mime_type, key):
        validate("SourceUploadMetadata", metadata)
        self.authorize(self.repo, actor, project_id, True)
        require(
            0 < len(raw) <= self.settings.max_upload_bytes,
            "UPLOAD_TOO_LARGE",
            "Upload must be nonempty and no larger than 10 MiB.",
            413,
        )
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        expected = {"txt": "text/plain", "md": "text/markdown", "pdf": "application/pdf"}.get(extension)
        require(
            expected is not None and mime_type in {expected, "application/octet-stream", "text/plain"},
            "UNSUPPORTED_MEDIA_TYPE",
            "Only TXT, Markdown and PDF evidence is supported.",
            415,
        )
        require(
            expected != "application/pdf" or raw.startswith(b"%PDF-"),
            "UNSUPPORTED_MEDIA_TYPE",
            "The document signature is not PDF.",
            415,
        )
        receipt = self.receipt_path(project_id, actor, "upload", key)
        request_hash = digest({"metadata": metadata, "sha256": digest(raw), "mime_type": expected})
        previous = self.replay(self.repo, receipt, request_hash)
        if previous:
            return self.source(actor, project_id, previous["target_id"])
        source_id = "src_" + digest([receipt, request_hash])[:32]
        original = self.store.put(f"projects/{project_id}/sources/{source_id}/original", raw, expected)

        def create(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            previous = self.replay(unit, receipt, request_hash)
            if previous:
                return previous["target_id"], None
            self.revisions(project, metadata["expected_evidence_revision"])
            require(
                project.get("_source_count", 0) < 100,
                "INPUT_LIMIT_EXCEEDED",
                "Project source capacity reached.",
                422,
            )
            unique = path(
                project_id, "versions", digest([metadata["document_id"], metadata["version_number"]])
            )
            require(unit.get(unique) is None, "VERSION_CONFLICT", "This document version already exists.")
            predecessor = metadata["supersedes_source_version_id"]
            if predecessor:
                old = self.entity(unit, project_id, "sources", predecessor)
                require(
                    old["document_id"] == metadata["document_id"],
                    "INVALID_STATE_TRANSITION",
                    "Source lineage must stay within one logical document.",
                    422,
                )
            source = {key: value for key, value in metadata.items() if key != "expected_evidence_revision"}
            source.update(
                {
                    "id": source_id,
                    "project_id": project_id,
                    "mime_type": expected,
                    "authority": "unreviewed",
                    "authority_confirmed_by": None,
                    "content_sha256": digest(raw),
                    "canonical_text_sha256": None,
                    "ingestion_status": "queued",
                    "ingestion_failure": None,
                    "page_count": None,
                    "extraction_method": None,
                    "processor_version": None,
                    "withdrawn_at": None,
                    "withdrawn_by": None,
                    "withdrawal_reason": None,
                    "_original": original,
                    "_filename": filename,
                    "_canonical": None,
                    "_page_spans": [],
                    "_processor_version": self.settings.processor_version
                    if expected == "application/pdf"
                    else None,
                }
            )
            job_id = self.new_job(unit, project_id, source_id, "ingestion", actor)
            source["_job_id"] = job_id
            unit.create(path(project_id, "sources", source_id), source)
            unit.create(unique, {"source_id": source_id})
            unit.create(receipt, {"request_hash": request_hash, "target_id": source_id})
            project["evidence_revision"] += 1
            project["_source_count"] = project.get("_source_count", 0) + 1
            unit.set(path(project_id), project)
            self.audit(
                unit,
                project,
                actor.user_id,
                "source_uploaded",
                source_id,
                "Immutable evidence queued for extraction.",
            )
            return source_id, job_id

        source_id, job_id = self.repo.transact(create)
        if job_id:
            self.dispatch(job_id)
        return self.source(actor, project_id, source_id)

    def review_source(self, actor, project_id, source_id, request):
        validate("SourceReviewRequest", request)

        def review(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            self.revisions(project, request["expected_evidence_revision"])
            source = self.entity(unit, project_id, "sources", source_id)
            require(
                source["ingestion_status"] == "ready", "SOURCE_NOT_READY", "Review requires ready extraction."
            )
            predecessor = request["supersedes_source_version_id"]
            if predecessor:
                require(
                    request["authority"] == "accepted_baseline" and request["effective_at"] is not None,
                    "INVALID_STATE_TRANSITION",
                    "Supersession requires a reviewed effective baseline.",
                    422,
                )
                visited = {source_id}
                cursor = predecessor
                while cursor:
                    require(cursor not in visited, "INVALID_STATE_TRANSITION", "Cyclic source lineage.", 422)
                    visited.add(cursor)
                    old = self.entity(unit, project_id, "sources", cursor)
                    require(
                        old["document_id"] == source["document_id"]
                        and old["authority"] == "accepted_baseline",
                        "INVALID_STATE_TRANSITION",
                        "Supersession requires same-document accepted lineage.",
                        422,
                    )
                    cursor = old["supersedes_source_version_id"]
            source.update(
                {key: request[key] for key in ("authority", "effective_at", "supersedes_source_version_id")}
            )
            source["authority_confirmed_by"] = actor.user_id if request["authority"] != "unreviewed" else None
            project["evidence_revision"] += 1
            unit.set(path(project_id, "sources", source_id), source)
            unit.set(path(project_id), project)
            self.audit(
                unit, project, actor.user_id, "source_authority_reviewed", source_id, request["reason"][:1000]
            )
            return source, project

        source, project = self.repo.transact(review)
        return {"source": self.source_view(source), "project": self.project_view(project)}

    def remedy_source(self, actor, project_id, source_id, request, key, withdraw=False):
        validate("SourceWithdrawRequest" if withdraw else "SourceRetryRequest", request)
        receipt = self.receipt_path(
            project_id, actor, f"{'withdraw' if withdraw else 'retry'}:{source_id}", key
        )
        request_hash = digest(request)

        def remedy(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            previous = self.replay(unit, receipt, request_hash)
            source = self.entity(unit, project_id, "sources", source_id)
            if previous:
                return source, project, None
            self.revisions(project, request["expected_evidence_revision"])
            job = unit.get(f"jobs/{source['_job_id']}")
            require(
                source["ingestion_status"] == "failed"
                and source["authority"] == "unreviewed"
                and source["withdrawn_at"] is None
                and job
                and job["status"] == "failed",
                "INVALID_STATE_TRANSITION",
                "Only terminal unreviewed failed evidence can be remedied.",
                422,
            )
            job_id = None
            if withdraw:
                source.update(
                    withdrawn_at=now(), withdrawn_by=actor.user_id, withdrawal_reason=request["reason"]
                )
            else:
                job_id = self.new_job(unit, project_id, source_id, "ingestion", actor)
                source.update(ingestion_status="queued", ingestion_failure=None, _job_id=job_id)
            project["evidence_revision"] += 1
            unit.set(path(project_id, "sources", source_id), source)
            unit.set(path(project_id), project)
            unit.create(receipt, {"request_hash": request_hash, "target_id": source_id})
            self.audit(
                unit,
                project,
                actor.user_id,
                "failed_source_withdrawn" if withdraw else "ingestion_retried",
                source_id,
                request.get("reason", "A new bounded ingestion job was queued.")[:1000],
            )
            return source, project, job_id

        source, project, job_id = self.repo.transact(remedy)
        if job_id:
            self.dispatch(job_id)
        return {"source": self.source_view(source), "project": self.project_view(project)}

    def start_analysis(self, actor, project_id, request, key):
        validate("AnalysisRequest", request)
        project, _ = self.authorize(self.repo, actor, project_id, True)
        receipt = self.receipt_path(project_id, actor, "analysis", key)
        request_hash = digest(request)
        previous = self.replay(self.repo, receipt, request_hash)
        if previous:
            return self.analysis(actor, project_id, previous["target_id"])
        self.revisions(project, request["expected_evidence_revision"])
        require(
            timestamp(request["evaluation_as_of"]) <= timestamp(business_now(project)),
            "INVALID_REQUEST",
            "Analysis time cannot be later than the project business clock.",
            422,
        )
        records = self.repo.list(path(project_id, "sources"), 101)
        active = [item for item in records if not item["withdrawn_at"]]
        require(
            active and all(item["ingestion_status"] == "ready" for item in active),
            "SOURCE_NOT_READY",
            "All active evidence must be ready before analysis.",
        )
        require(
            len(active) <= self.settings.max_analysis_sources,
            "INPUT_LIMIT_EXCEEDED",
            "The complete packet exceeds the source limit.",
            422,
        )
        sources = [self.source_view(item) for item in active]
        require(
            sum(len(item["canonical_text"]) for item in sources) <= self.settings.max_analysis_chars,
            "INPUT_LIMIT_EXCEEDED",
            "The complete packet exceeds the character limit.",
            422,
        )
        withdrawn_ids = sorted(item["id"] for item in records if item["withdrawn_at"])
        warnings = []
        # Group IDs into bounded messages; all excluded failures remain disclosed.
        for offset in range(0, len(withdrawn_ids), 5):
            warnings.append(
                "Withdrawn failed sources excluded from analysis coverage: "
                + ", ".join(withdrawn_ids[offset : offset + 5])
            )
        run_id = "run_" + digest([receipt, request_hash])[:32]
        memberships = self.repo.list(path(project_id, "memberships"), 101)
        packet = {
            "project": self.project_view(project),
            "evidence_revision": project["evidence_revision"],
            "evaluation_as_of": request["evaluation_as_of"],
            "sources": sources,
            "commitments": public(self.repo.list(path(project_id, "commitments"), 501)),
            "memberships": public(memberships),
            "coverage_warnings": warnings,
        }
        snapshot = self.store.put(
            f"projects/{project_id}/analyses/{run_id}/snapshot.json", canonical(packet), "application/json"
        )

        def create(unit):
            current, _ = self.authorize(unit, actor, project_id, True)
            previous = self.replay(unit, receipt, request_hash)
            if previous:
                return previous["target_id"], None
            self.revisions(current, request["expected_evidence_revision"])
            run = {
                "id": run_id,
                "project_id": project_id,
                "provider": "vertex",
                "model": self.settings.model,
                "prompt_version": "handoff-guardian-v1",
                "evidence_revision": current["evidence_revision"],
                "evaluation_as_of": request["evaluation_as_of"],
                "source_version_ids": [item["id"] for item in sources],
                "status": "queued",
                "created_at": now(),
                "started_at": None,
                "completed_at": None,
                "warnings": warnings,
                "findings": [],
                "candidate_commitments": [],
                "failure": None,
                "_snapshot": snapshot,
                "_baseline_ids": active_baseline(records, request["evaluation_as_of"]),
                "_business_date": timestamp(request["evaluation_as_of"])
                .astimezone(ZoneInfo(current["timezone"]))
                .date()
                .isoformat(),
            }
            job_id = self.new_job(unit, project_id, run_id, "analysis", actor, snapshot)
            run["_job_id"] = job_id
            unit.create(path(project_id, "analyses", run_id), run)
            unit.create(receipt, {"request_hash": request_hash, "target_id": run_id})
            self.audit(
                unit,
                current,
                actor.user_id,
                "analysis_queued",
                run_id,
                "Authorized evidence snapshot queued.",
            )
            return run_id, job_id

        run_id, job_id = self.repo.transact(create)
        if job_id:
            self.dispatch(job_id)
        return self.analysis(actor, project_id, run_id)

    def run_view(self, project_id, run):
        value = public(run)
        if run.get("_result"):
            result = json.loads(self.store.read(run["_result"]))
            value["findings"] = result["findings"]
            value["candidate_commitments"] = result["candidate_commitments"]
        return value

    def analysis(self, actor, project_id, run_id):
        self.authorize(self.repo, actor, project_id)
        return self.run_view(project_id, self.entity(self.repo, project_id, "analyses", run_id))

    def check_action_current(self, unit, project, action):
        binding = action["binding"]
        self.revisions(project, binding["evidence_revision"], binding["base_board_revision"])
        require(
            digest(action["payload"]) == binding["payload_hash"], "STALE_TARGET", "Payload hash mismatch."
        )
        finding = self.entity(unit, project["id"], "findings", action["payload"]["finding_id"])
        run = self.entity(unit, project["id"], "analyses", action["analysis_run_id"])
        sources = unit.list(path(project["id"], "sources"), 101)
        current_date = (
            timestamp(business_now(project)).astimezone(ZoneInfo(project["timezone"])).date().isoformat()
        )
        require(
            finding["status"] == "open"
            and finding["version"] == binding["finding_version"]
            and finding["analysis_run_id"] == run["id"],
            "STALE_TARGET",
            "Finding changed; review again.",
        )
        require(
            run["status"] == "succeeded"
            and project.get("_current_analysis_run_id") == run["id"]
            and run["evidence_revision"] == project["evidence_revision"]
            and run.get("_baseline_ids") == active_baseline(sources, business_now(project))
            and run.get("_business_date") == current_date,
            "STALE_EVIDENCE",
            "The successful analysis no longer describes the current effective packet.",
        )
        require(
            unit.get(path(project["id"], "memberships", action["payload"]["owner_id"])) is not None,
            "STALE_TARGET",
            "The proposed owner is no longer a project member.",
        )

    def propose(self, actor, project_id, request):
        validate("ActionCreateRequest", request)
        action_id = identifier("act")

        def create(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            self.revisions(project, request["expected_evidence_revision"], request["expected_board_revision"])
            require(
                project.get("_action_count", 0) < 500,
                "INPUT_LIMIT_EXCEEDED",
                "Proposal capacity reached.",
                422,
            )
            action = {
                "id": action_id,
                "project_id": project_id,
                "analysis_run_id": request["analysis_run_id"],
                "type": "create_blocker_task",
                "payload": request["payload"],
                "binding": {
                    "payload_hash": digest(request["payload"]),
                    "evidence_revision": request["expected_evidence_revision"],
                    "base_board_revision": request["expected_board_revision"],
                    "finding_version": request["expected_finding_version"],
                },
                "status": "proposed",
                "created_by": actor.user_id,
                "created_at": now(),
                "replaces_action_id": request["replaces_action_id"],
                "approval": None,
                "task_id": None,
                "_payload_bytes": canonical(request["payload"]).decode(),
            }
            self.check_action_current(unit, project, action)
            if request["replaces_action_id"]:
                old = self.entity(unit, project_id, "action_proposals", request["replaces_action_id"])
                require(
                    old["status"] != "executed",
                    "INVALID_STATE_TRANSITION",
                    "Executed actions cannot be replaced.",
                    422,
                )
                old["status"] = "stale"
                unit.set(path(project_id, "action_proposals", old["id"]), old)
            unit.create(path(project_id, "action_proposals", action_id), action)
            project["_action_count"] = project.get("_action_count", 0) + 1
            unit.set(path(project_id), project)
            self.audit(
                unit,
                project,
                actor.user_id,
                "action_proposed",
                action_id,
                "Immutable blocker task proposed for separate review.",
                action["binding"]["payload_hash"],
            )
            return action

        return public(self.repo.transact(create))

    def review_action(self, actor, project_id, action_id, request):
        validate("ActionReviewRequest", request)

        def review(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            action = self.entity(unit, project_id, "action_proposals", action_id)
            require(action["binding"] == request["binding"], "STALE_TARGET", "The submitted binding differs.")
            require(
                action["status"] == "proposed",
                "INVALID_STATE_TRANSITION",
                "Only a proposed action can be reviewed.",
                422,
            )
            try:
                self.check_action_current(unit, project, action)
            except AppError as exc:
                action["status"] = "stale"
                unit.set(path(project_id, "action_proposals", action_id), action)
                self.audit(unit, project, actor.user_id, "action_stale", action_id, exc.message)
                return exc
            action["approval"] = {
                "id": identifier("apr"),
                "reviewer_id": actor.user_id,
                "decision": request["decision"],
                "note": request["note"],
                "reviewed_at": now(),
                "binding": request["binding"],
            }
            action["_approver_binding_id"] = actor.binding_id
            action["_approver_binding_version"] = actor.binding_version
            action["status"] = "approved" if request["decision"] == "approve" else "rejected"
            unit.set(path(project_id, "action_proposals", action_id), action)
            self.audit(
                unit,
                project,
                actor.user_id,
                "action_reviewed",
                action_id,
                "Immutable action reviewed.",
                request["binding"]["payload_hash"],
            )
            return action

        result = self.repo.transact(review)
        if isinstance(result, AppError):
            raise result
        return public(result)

    def execute(self, actor, project_id, action_id, binding, key):
        validate("ActionBinding", binding)
        receipt = self.receipt_path(project_id, actor, "execute", key)
        request_hash = digest({"action_id": action_id, "binding": binding})
        task_id = "task_" + digest([project_id, action_id])[:32]

        def execute(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            previous = self.replay(unit, receipt, request_hash)
            action = self.entity(unit, project_id, "action_proposals", action_id)
            task = unit.get(path(project_id, "tasks", task_id))
            require(
                action["binding"] == binding,
                "STALE_TARGET",
                "The submitted binding differs from the immutable action.",
            )
            if action["status"] == "executed":
                require(
                    task is not None and action["task_id"] == task_id,
                    "INVALID_STATE_TRANSITION",
                    "The executed task record is unavailable.",
                    422,
                )
                if not previous:
                    unit.create(receipt, {"request_hash": request_hash, "target_id": task_id})
                return {"action": public(action), "task": public(task), "replayed": True}
            require(
                action["status"] == "approved"
                and action["approval"]
                and action["approval"]["decision"] == "approve",
                "ACTION_NOT_APPROVED",
                "The action requires approval.",
            )
            require(
                action["approval"]["binding"] == binding
                and action["_payload_bytes"].encode() == canonical(action["payload"]),
                "STALE_TARGET",
                "Approval or immutable payload differs.",
            )
            require(
                project.get("_task_count", 0)
                + project.get("_open_finding_count", 0)
                + project.get("_current_reviewed_finding_count", 0)
                < 500,
                "INPUT_LIMIT_EXCEEDED",
                "Project board capacity reached.",
                422,
            )
            approver = Actor(
                action["approval"]["reviewer_id"],
                action["_approver_binding_id"],
                action["_approver_binding_version"],
            )
            try:
                self.authorize(unit, approver, project_id, True)
                self.check_action_current(unit, project, action)
            except AppError as exc:
                action["status"] = "stale"
                unit.set(path(project_id, "action_proposals", action_id), action)
                self.audit(
                    unit,
                    project,
                    actor.user_id,
                    "action_stale",
                    action_id,
                    exc.message,
                    binding["payload_hash"],
                )
                return AppError(
                    "STALE_EVIDENCE" if exc.code == "STALE_EVIDENCE" else "STALE_TARGET",
                    "The approval is stale; create and review a fresh proposal.",
                )
            require(task is None, "VERSION_CONFLICT", "A task already exists for this action.")
            task = {
                "id": task_id,
                "project_id": project_id,
                "action_proposal_id": action_id,
                **{key: action["payload"][key] for key in ("title", "description", "owner_id", "due_date")},
                "status": "open",
                "version": 1,
            }
            action.update(status="executed", task_id=task_id, _executed_by=actor.user_id, _executed_at=now())
            project["board_revision"] += 1
            project["_task_count"] = project.get("_task_count", 0) + 1
            unit.create(path(project_id, "tasks", task_id), task)
            unit.set(path(project_id, "action_proposals", action_id), action)
            unit.set(path(project_id), project)
            unit.create(receipt, {"request_hash": request_hash, "target_id": task_id})
            self.audit(
                unit,
                project,
                actor.user_id,
                "blocker_task_created",
                task_id,
                "Approved blocker task persisted; finding and commitment states are unchanged.",
                binding["payload_hash"],
            )
            return {"action": public(action), "task": task, "replayed": False}

        result = self.repo.transact(execute)
        if isinstance(result, AppError):
            raise result
        return result

    def complete_task(self, actor, project_id, task_id, request, key):
        validate("TaskCompleteRequest", request)
        receipt = self.receipt_path(project_id, actor, "complete_task", key)
        request_hash = digest({"task_id": task_id, **request})

        def complete(unit):
            project, _ = self.authorize(unit, actor, project_id, True)
            previous = self.replay(unit, receipt, request_hash)
            task = self.entity(unit, project_id, "tasks", task_id)
            if previous:
                return public(task)
            self.revisions(project, board=request["expected_board_revision"])
            require(
                task["version"] == request["expected_task_version"],
                "VERSION_CONFLICT",
                "The task changed; refresh before marking it done.",
            )
            if task["status"] != "done":
                require(
                    task["status"] in ("open", "in_progress"),
                    "INVALID_STATE_TRANSITION",
                    "Only an open or in-progress task can be completed.",
                    422,
                )
                task.update(status="done", version=task["version"] + 1)
                project["board_revision"] += 1
                unit.set(path(project_id, "tasks", task_id), task)
                unit.set(path(project_id), project)
                self.audit(
                    unit, project, actor.user_id, "task_completed", task_id,
                    "Reviewer marked the task done; finding and commitment states are unchanged.",
                )
            unit.create(receipt, {"request_hash": request_hash, "target_id": task_id})
            return public(task)

        return self.repo.transact(complete)

    def review_entity(self, actor, project_id, entity_id, request, commitment=False):
        validate("CommitmentReviewRequest" if commitment else "FindingReviewRequest", request)
        project, _ = self.authorize(self.repo, actor, project_id, True)
        self.revisions(project, request["expected_evidence_revision"])
        sources = {}
        for span in request["evidence"]:
            if span["source_version_id"] not in sources:
                sources[span["source_version_id"]] = self.source(actor, project_id, span["source_version_id"])
            check_span(span, sources)
        if commitment:
            authority = "accepted_baseline" if request["decision"] == "accept" else "acceptance_evidence"
            require(
                all(
                    source["authority"] == authority and source["authority_confirmed_by"]
                    for source in sources.values()
                ),
                "INVALID_EVIDENCE",
                "Commitment review requires evidence with the reviewed authority.",
                422,
            )
            if request["decision"] == "close":
                require(
                    all(
                        not source["effective_at"]
                        or timestamp(source["effective_at"]) <= timestamp(business_now(project))
                        for source in sources.values()
                    ),
                    "INVALID_EVIDENCE",
                    "Future-effective acceptance evidence cannot establish closure.",
                    422,
                )
        else:
            require(
                request["decision"] != "resolve" or bool(request["evidence"]),
                "INVALID_EVIDENCE",
                "Finding resolution requires supporting evidence.",
                422,
            )

        def review(unit):
            current, _ = self.authorize(unit, actor, project_id, True)
            self.revisions(current, request["expected_evidence_revision"])
            collection = "commitments" if commitment else "findings"
            entity = self.entity(unit, project_id, collection, entity_id)
            version_key = "expected_commitment_version" if commitment else "expected_finding_version"
            require(
                entity["version"] == request[version_key], "VERSION_CONFLICT", "The reviewed item changed."
            )
            # Re-check the same immutable source hashes and reviewed metadata in the transaction.
            for source in sources.values():
                actual = self.entity(unit, project_id, "sources", source["id"])
                require(
                    actual["canonical_text_sha256"] == source["canonical_text_sha256"]
                    and actual["authority"] == source["authority"],
                    "STALE_EVIDENCE",
                    "Evidence review changed.",
                )
            superseded = None
            if commitment:
                accept = request["decision"] == "accept"
                require(
                    entity["status"] == ("proposed" if accept else "accepted"),
                    "INVALID_STATE_TRANSITION",
                    "Commitment cannot make this transition.",
                    422,
                )
                predecessor = request["supersedes_commitment_id"]
                require(
                    accept or predecessor is None,
                    "INVALID_STATE_TRANSITION",
                    "Closure cannot supersede a term.",
                    422,
                )
                if predecessor:
                    require(
                        predecessor != entity_id,
                        "INVALID_STATE_TRANSITION",
                        "A commitment cannot supersede itself.",
                        422,
                    )
                    superseded = self.entity(unit, project_id, collection, predecessor)
                    require(
                        superseded["status"] == "accepted",
                        "INVALID_STATE_TRANSITION",
                        "Predecessor is not accepted.",
                        422,
                    )
                    superseded.update(status="superseded", version=superseded["version"] + 1)
                    entity["supersedes_commitment_id"] = predecessor
                if accept:
                    baseline = active_baseline(
                        unit.list(path(project_id, "sources"), 101), business_now(current)
                    )
                    require(
                        set(sources) <= set(baseline),
                        "INVALID_EVIDENCE",
                        "Acceptance requires currently effective baseline evidence.",
                        422,
                    )
                entity["status"] = "accepted" if accept else "closed"
                entity["evidence"] = request["evidence"]
                current["evidence_revision"] += 1
                unit.set(path(project_id), current)
            else:
                require(
                    entity["status"] == "open",
                    "INVALID_STATE_TRANSITION",
                    "The finding is already reviewed.",
                    422,
                )
                entity["status"] = "dismissed" if request["decision"] == "dismiss" else "resolved"
                current["_open_finding_count"] = max(0, current.get("_open_finding_count", 0) - 1)
                if entity["analysis_run_id"] == current.get("_current_analysis_run_id"):
                    current["_current_reviewed_finding_count"] = (
                        current.get("_current_reviewed_finding_count", 0) + 1
                    )
                unit.set(path(project_id), current)
            entity["version"] += 1
            unit.set(path(project_id, collection, entity_id), entity)
            if superseded:
                unit.set(path(project_id, collection, superseded["id"]), superseded)
            self.audit(
                unit,
                current,
                actor.user_id,
                "commitment_reviewed" if commitment else "finding_reviewed",
                entity_id,
                request["reason"][:1000],
            )
            if commitment:
                return {
                    "commitment": public(entity),
                    "superseded_commitment": public(superseded),
                    "project": self.project_view(current),
                }
            return public(entity)

        return self.repo.transact(review)
