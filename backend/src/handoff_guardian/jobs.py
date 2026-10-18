"""Durable at-least-once jobs, leased claims, outbox recovery and fenced publication."""

import json
import time
from datetime import UTC, datetime, timedelta

from .contracts import canonical, digest
from .errors import AppError, require
from .evidence import canonicalize_text, now, timestamp, validate_provider
from .observability import emit
from .ports import Actor, ProviderResponse
from .service import Guardian, identifier, path


class JobRunner:
    def __init__(self, guardian: Guardian, extractor, provider):
        self.guardian, self.repo, self.store = guardian, guardian.repo, guardian.store
        self.extractor, self.provider = extractor, provider
        self.settings = guardian.settings

    @staticmethod
    def actor(job):
        return Actor(job["actor_id"], job["binding_id"], job["binding_version"])

    def claim(self, job_id, generation):
        def claim(unit):
            job = unit.get(f"jobs/{job_id}")
            require(job is not None, "NOT_FOUND", "Job not found.", 404)
            if job["status"] in {"completed", "failed", "stale"}:
                return "already_terminal"
            if job["dispatch_generation"] != generation:
                return "ignored_outdated"
            self.guardian.authorize(unit, self.actor(job), job["project_id"], True)
            require(
                not job["lease_until"] or timestamp(job["lease_until"]) <= datetime.now(UTC),
                "JOB_LEASE_ACTIVE",
                "Another durable attempt still holds this lease.",
                503,
            )
            require(
                job["attempt_count"] < self.settings.job_max_attempts,
                "RETRY_BUDGET_EXHAUSTED",
                "The durable attempt budget is exhausted.",
                422,
            )
            job["attempt_count"] += 1
            job["fencing_token"] += 1
            job["lease_until"] = (
                datetime.now(UTC) + timedelta(seconds=self.settings.job_lease_seconds)
            ).isoformat()
            job["status"] = "running"
            job["_provider_usage"] = "unknown"
            collection = "sources" if job["kind"] == "ingestion" else "analyses"
            target = self.guardian.entity(unit, job["project_id"], collection, job["target_id"])
            target["ingestion_status" if job["kind"] == "ingestion" else "status"] = (
                "processing" if job["kind"] == "ingestion" else "running"
            )
            if job["kind"] == "analysis":
                target["started_at"] = now()
            unit.set(path(job["project_id"], collection, job["target_id"]), target)
            unit.set(f"jobs/{job_id}", job)
            return job

        return self.repo.transact(claim)

    async def run(self, job_id, generation):
        started = time.monotonic()
        try:
            result = await self._run(job_id, generation)
        except AppError as exc:
            self.record_job(
                job_id,
                "retryable_error" if exc.status == 503 else "rejected",
                started,
                fallback_code=exc.code,
            )
            raise
        except Exception:
            self.record_job(job_id, "dependency_error", started, fallback_code="TEMPORARY_DEPENDENCY_FAILURE")
            raise
        self.record_job(job_id, result["outcome"], started)
        return result

    def record_job(self, job_id, outcome, started, fallback_code=None):
        try:
            durable = self.repo.get(f"jobs/{job_id}") or {}
        except Exception:
            durable = {}
        status = durable.get("status", "unavailable")
        emit(
            "job_failed" if status == "failed" else "job_outcome",
            job_id=job_id,
            project_id=durable.get("project_id"),
            target_id=durable.get("target_id"),
            kind=durable.get("kind"),
            status=status,
            outcome=outcome,
            attempt_count=durable.get("attempt_count"),
            dispatch_generation=durable.get("dispatch_generation"),
            fencing_token=durable.get("fencing_token"),
            code=durable.get("_failure_code", fallback_code),
            latency_ms=round((time.monotonic() - started) * 1000),
        )

    async def _run(self, job_id, generation):
        try:
            job = self.claim(job_id, generation)
        except AppError as exc:
            if exc.code in {
                "IDENTITY_NOT_PROVISIONED",
                "NOT_FOUND",
                "REVIEWER_REQUIRED",
                "RETRY_BUDGET_EXHAUSTED",
            }:
                self.terminate_unclaimable(job_id, exc.code)
                return {"job_id": job_id, "outcome": "failed"}
            raise
        if isinstance(job, str):
            return {"job_id": job_id, "outcome": job}
        emit(
            "job_started",
            job_id=job_id,
            project_id=job["project_id"],
            target_id=job["target_id"],
            kind=job["kind"],
            status=job["status"],
            attempt_count=job["attempt_count"],
            dispatch_generation=job["dispatch_generation"],
            fencing_token=job["fencing_token"],
        )
        try:
            if job["kind"] == "ingestion":
                outcome = self.ingest(job)
            else:
                outcome = await self.analyze(job)
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else "TEMPORARY_DEPENDENCY_FAILURE"
            if isinstance(exc, TimeoutError):
                code = "PROVIDER_TIMEOUT"
            terminal = isinstance(exc, AppError) and exc.status != 503
            outcome = self.fail(job, code, terminal)
        if outcome == "retry_scheduled":
            raise AppError("TEMPORARY_DEPENDENCY_FAILURE", "A durable retry was scheduled for this job.", 503)
        return {"job_id": job_id, "outcome": outcome}

    def fenced(self, unit, captured):
        job = unit.get(f"jobs/{captured['id']}")
        return (
            job is not None
            and job["status"] == "running"
            and job["fencing_token"] == captured["fencing_token"]
            and job["dispatch_generation"] == captured["dispatch_generation"]
            and timestamp(job["lease_until"]) > datetime.now(UTC)
        )

    def ingest(self, job):
        project_id, source_id = job["project_id"], job["target_id"]
        self.guardian.authorize(self.repo, self.actor(job), project_id, True)
        source = self.guardian.entity(self.repo, project_id, "sources", source_id)
        raw = self.store.read(source["_original"])
        if source["mime_type"] == "application/pdf":
            require(
                source["_processor_version"] == self.settings.processor_version,
                "EXTRACTION_FAILED",
                "The immutable extraction configuration changed.",
                422,
            )
            text, pages, processor = self.extractor.extract(raw)
            processor_ref = self.store.put(
                f"projects/{project_id}/sources/{source_id}/attempts/{job['id']}-{job['fencing_token']}/processor.json",
                processor,
                "application/json",
            )
        else:
            text, pages, processor_ref = canonicalize_text(raw), [], None
        canonical_ref = self.store.put(
            f"projects/{project_id}/sources/{source_id}/attempts/{job['id']}-{job['fencing_token']}/canonical.txt",
            text.encode(),
            "text/plain",
        )

        def publish(unit):
            if not self.fenced(unit, job):
                return "stale"
            project, _ = self.guardian.authorize(unit, self.actor(job), project_id, True)
            current = self.guardian.entity(unit, project_id, "sources", source_id)
            require(
                current["_job_id"] == job["id"] and current["_original"] == source["_original"],
                "STALE_EVIDENCE",
                "Ingestion ownership changed.",
            )
            current.update(
                ingestion_status="ready",
                ingestion_failure=None,
                canonical_text_sha256=digest(text.encode()),
                page_count=len(pages) if pages else None,
                extraction_method="document_ai_ocr" if pages else "utf8",
                processor_version=self.settings.processor_version if pages else None,
                _canonical=canonical_ref,
                _page_spans=pages,
                _processor=processor_ref,
            )
            project["evidence_revision"] += 1
            self.complete_job(unit, job)
            unit.set(path(project_id, "sources", source_id), current)
            unit.set(path(project_id), project)
            self.guardian.audit(
                unit,
                project,
                job["actor_id"],
                "source_extraction_ready",
                source_id,
                "Immutable canonical evidence published.",
            )
            return "completed"

        return self.repo.transact(publish)

    async def analyze(self, job):
        project_id, run_id = job["project_id"], job["target_id"]
        project, _ = self.guardian.authorize(self.repo, self.actor(job), project_id, True)
        packet = json.loads(self.store.read(job["snapshot"]))
        require(packet["project"]["id"] == project_id, "INVALID_EVIDENCE", "Snapshot project mismatch.", 422)
        self.guardian.revisions(project, packet["evidence_revision"])
        for item in [*packet["sources"], *packet["commitments"]]:
            require(item["project_id"] == project_id, "INVALID_EVIDENCE", "Foreign snapshot reference.", 422)
        response = await self.provider.analyze(packet, f"{project_id}:{run_id}:{job['fencing_token']}")
        result = response.result if isinstance(response, ProviderResponse) else response
        provenance = response.provenance if isinstance(response, ProviderResponse) else {"usage": "unknown"}
        validate_provider(result, packet)
        warnings = list(dict.fromkeys(packet["coverage_warnings"] + result["coverage_warnings"]))
        require(
            len(warnings) <= 50,
            "PROVIDER_OUTPUT_INVALID",
            "Combined coverage warnings exceed the bound.",
            422,
        )
        candidate_map = {
            item["id"]: "cmt_" + digest([run_id, item["id"]])[:32] for item in result["proposed_commitments"]
        }
        candidates = [
            {
                **item,
                "id": candidate_map[item["id"]],
                "project_id": project_id,
                "status": "proposed",
                "version": 1,
                "supersedes_commitment_id": None,
            }
            for item in result["proposed_commitments"]
        ]
        findings, previous_findings = [], {}
        for item in result["findings"]:
            signature = digest([project_id, item["kind"], sorted(item["evidence"], key=canonical)])
            finding_id = "fnd_" + signature[:32]
            require(
                finding_id not in previous_findings,
                "PROVIDER_OUTPUT_INVALID",
                "The agent returned duplicate observations with identical grounding.",
                422,
            )
            previous = self.repo.get(path(project_id, "findings", finding_id))
            previous_findings[finding_id] = previous
            findings.append(
                {
                    **item,
                    "id": finding_id,
                    "project_id": project_id,
                    "analysis_run_id": run_id,
                    "status": previous["status"] if previous else "open",
                    "version": previous["version"] + 1 if previous else 1,
                    "commitment_ids": [candidate_map.get(value, value) for value in item["commitment_ids"]],
                }
            )
        result_ref = self.store.put(
            f"projects/{project_id}/analyses/{run_id}/attempts/{job['fencing_token']}/result.json",
            canonical({"findings": findings, "candidate_commitments": candidates}),
            "application/json",
        )

        def publish(unit):
            if not self.fenced(unit, job):
                return "stale"
            current, _ = self.guardian.authorize(unit, self.actor(job), project_id, True)
            run = self.guardian.entity(unit, project_id, "analyses", run_id)
            fresh_findings = {
                finding_id: unit.get(path(project_id, "findings", finding_id))
                for finding_id in previous_findings
            }
            new_open = sum(previous is None for previous in previous_findings.values())
            current_reviewed = sum(item["status"] != "open" for item in findings)
            require(
                current.get("_task_count", 0)
                + current.get("_open_finding_count", 0)
                + new_open
                + current_reviewed
                <= 500
                and current.get("_commitment_count", 0) + len(candidates) <= 500,
                "INPUT_LIMIT_EXCEEDED",
                "Analysis would exceed project entity capacity.",
                422,
            )
            stale = (
                current["evidence_revision"] != run["evidence_revision"]
                or fresh_findings != previous_findings
            )
            run.update(
                status="stale" if stale else "succeeded",
                completed_at=now(),
                _result=result_ref,
                _provider_provenance=provenance,
                warnings=warnings,
            )
            unit.set(path(project_id, "analyses", run_id), run)
            if not stale:
                for value in candidates:
                    unit.create(path(project_id, "commitments", value["id"]), value)
                for value in findings:
                    if previous_findings[value["id"]]:
                        unit.set(path(project_id, "findings", value["id"]), value)
                        self.guardian.audit(
                            unit,
                            current,
                            job["actor_id"],
                            "finding_observed_again",
                            value["id"],
                            "Identical evidence observed again; reviewer disposition preserved.",
                        )
                    else:
                        unit.create(path(project_id, "findings", value["id"]), value)
                current["_current_analysis_run_id"] = run_id
                current["_current_finding_count"] = len(findings)
                current["_open_finding_count"] = current.get("_open_finding_count", 0) + new_open
                current["_current_reviewed_finding_count"] = current_reviewed
                current["_commitment_count"] = current.get("_commitment_count", 0) + len(candidates)
                unit.set(path(project_id), current)
            self.complete_job(unit, job, "stale" if stale else "completed")
            self.guardian.audit(
                unit,
                current,
                job["actor_id"],
                "analysis_stale" if stale else "analysis_published",
                run_id,
                "Validated Agent Engine analysis archived"
                + (" as historical." if stale else " and published."),
            )
            return "stale" if stale else "completed"

        return self.repo.transact(publish)

    @staticmethod
    def complete_job(unit, job, status="completed"):
        current = dict(job)
        current.update(status=status, lease_until=None, completed_at=now())
        unit.set(f"jobs/{job['id']}", current)

    def fail(self, job, code, terminal=False):
        def fail(unit):
            if not self.fenced(unit, job):
                return "stale"
            current = unit.get(f"jobs/{job['id']}")
            terminal_now = terminal or current["attempt_count"] >= self.settings.job_max_attempts
            project = unit.get(path(job["project_id"]))
            collection = "sources" if job["kind"] == "ingestion" else "analyses"
            target = unit.get(path(job["project_id"], collection, job["target_id"]))
            current["lease_until"] = None
            current["_failure_code"] = code
            if terminal_now:
                current["status"] = "failed"
                failure = {
                    "code": code,
                    "message": "Processing failed; inspect the original and retry after resolving the issue.",
                    "request_id": identifier("req"),
                }
                target["ingestion_status" if job["kind"] == "ingestion" else "status"] = "failed"
                target["ingestion_failure" if job["kind"] == "ingestion" else "failure"] = failure
                if job["kind"] == "ingestion":
                    project["evidence_revision"] += 1
                    unit.set(path(project["id"]), project)
                else:
                    target["completed_at"] = now()
                self.guardian.audit(
                    unit,
                    project,
                    job["actor_id"],
                    "job_failed",
                    job["target_id"],
                    f"Processing failed with safe code {code}.",
                )
            else:
                current["status"] = "pending"
                current["pending_since"] = now()
                current["dispatch_generation"] += 1
                target["ingestion_status" if job["kind"] == "ingestion" else "status"] = "queued"
                unit.set(
                    f"outbox/{job['id']}",
                    {"job_id": job["id"], "generation": current["dispatch_generation"], "status": "pending"},
                )
            unit.set(f"jobs/{job['id']}", current)
            unit.set(path(job["project_id"], collection, job["target_id"]), target)
            return "failed" if terminal_now else "retry_scheduled"

        outcome = self.repo.transact(fail)
        if outcome == "retry_scheduled":
            self.guardian.dispatch(job["id"])
        return outcome

    def terminate_unclaimable(self, job_id, code, captured=None):
        def terminate(unit):
            job = unit.get(f"jobs/{job_id}")
            if not job or job["status"] in {"completed", "failed", "stale"}:
                return
            if captured and (
                job["fencing_token"] != captured["fencing_token"]
                or not job["lease_until"]
                or timestamp(job["lease_until"]) > datetime.now(UTC)
            ):
                return
            project = unit.get(path(job["project_id"]))
            collection = "sources" if job["kind"] == "ingestion" else "analyses"
            target = unit.get(path(job["project_id"], collection, job["target_id"]))
            job.update(status="failed", lease_until=None, _failure_code=code)
            unit.set(f"jobs/{job_id}", job)
            if target:
                target["ingestion_status" if job["kind"] == "ingestion" else "status"] = "failed"
                target["ingestion_failure" if job["kind"] == "ingestion" else "failure"] = {
                    "code": code,
                    "message": "The processing request is no longer authorized or has exhausted its budget.",
                    "request_id": identifier("req"),
                }
                unit.set(path(job["project_id"], collection, job["target_id"]), target)
                if project and job["kind"] == "ingestion":
                    project["evidence_revision"] += 1
                    unit.set(path(project["id"]), project)

        self.repo.transact(terminate)

    def recover(self):
        started = time.monotonic()
        try:
            result = self._recover()
        except Exception:
            emit(
                "recovery_failed",
                status="retryable_error",
                code="TEMPORARY_DEPENDENCY_FAILURE",
                latency_ms=round((time.monotonic() - started) * 1000),
            )
            raise
        emit(
            "recovery_completed",
            status="completed",
            **result,
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        return result

    def _recover(self):
        counts = {"enqueued": 0, "expired_jobs": 0, "failed_jobs": 0}
        for job in self.repo.where("jobs", "status", "running", 100):
            if job["status"] == "running" and timestamp(job["lease_until"]) <= datetime.now(UTC):
                started = time.monotonic()
                counts["expired_jobs"] += 1
                if job["attempt_count"] >= self.settings.job_max_attempts:
                    self.terminate_unclaimable(job["id"], "RETRY_BUDGET_EXHAUSTED", captured=job)
                    self.record_job(job["id"], "recovery_budget_exhausted", started)
                    counts["failed_jobs"] += 1
                    continue

                def requeue(unit, captured=job):
                    current = unit.get(f"jobs/{captured['id']}")
                    if (
                        current["status"] == "running"
                        and current["fencing_token"] == captured["fencing_token"]
                        and timestamp(current["lease_until"]) <= datetime.now(UTC)
                    ):
                        current.update(
                            status="pending",
                            lease_until=None,
                            dispatch_generation=current["dispatch_generation"] + 1,
                            pending_since=now(),
                        )
                        unit.set(f"jobs/{current['id']}", current)
                        unit.set(
                            f"outbox/{current['id']}",
                            {
                                "job_id": current["id"],
                                "generation": current["dispatch_generation"],
                                "status": "pending",
                            },
                        )

                self.repo.transact(requeue)
                self.record_job(job["id"], "recovery_requeued", started)
        # A queue can exhaust its own delivery retries without a worker ever claiming a job.
        # Refresh a long-pending generation; terminal records never consume this bounded scan.
        for job in self.repo.where("jobs", "status", "pending", 100):
            if (
                datetime.now(UTC) - timestamp(job.get("pending_since", job["created_at"]))
            ).total_seconds() < 360:
                continue

            def redispatch(unit, captured=job):
                current = unit.get(f"jobs/{captured['id']}")
                outbox = unit.get(f"outbox/{captured['id']}")
                if (
                    current["status"] == "pending"
                    and outbox
                    and outbox["status"] == "dispatched"
                    and current["dispatch_generation"] == captured["dispatch_generation"]
                ):
                    current.update(
                        dispatch_generation=current["dispatch_generation"] + 1, pending_since=now()
                    )
                    unit.set(f"jobs/{current['id']}", current)
                    unit.set(
                        f"outbox/{current['id']}",
                        {
                            "job_id": current["id"],
                            "generation": current["dispatch_generation"],
                            "status": "pending",
                        },
                    )

            self.repo.transact(redispatch)
        for record in self.repo.where("outbox", "status", "pending", 100):
            if record["status"] == "pending":
                started = time.monotonic()
                self.guardian.dispatch(record["job_id"])
                latest = self.repo.get(f"outbox/{record['job_id']}")
                counts["enqueued"] += int(latest["status"] == "dispatched")
                self.record_job(
                    record["job_id"],
                    "outbox_dispatched" if latest["status"] == "dispatched" else "outbox_pending",
                    started,
                )
        return counts
