#!/usr/bin/env python3
"""Run real deployed acceptance probes and emit credential-free JSON evidence.

Unperformed manual, destructive-fault, browser, or contention observations remain
blocked. A partial probe never passes its larger acceptance case. See the runbook
in infra/terraform/README.md and docs/CLOUD_ACCEPTANCE.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap_reviewer import cloud_client, identifier, timestamp

CASES = {
    "DEPLOY-01": "Inspect Cloud Build, all three Run revisions, agent resource and private worker IAM.",
    "AUTH-01": "Verify email/password login, refresh, and UI/server identity agreement in the browser.",
    "AUTH-02": "Reject missing, invalid, expired and wrong-project user tokens and demo actor headers.",
    "AUTH-03": "Reject viewer writes, nonmember reads, and execution after reviewer membership revocation.",
    "WORKER-01": "Reject unauthenticated/unapproved/user-token calls and observe actual Tasks OIDC delivery.",
    "PDF-01": "Upload Harbour PDF via Document AI, inspect canonical text and review authority.",
    "PDF-02": "Process scanned and 15-page PDFs; reject encrypted, 16-page, oversized and mismatched inputs.",
    "PDF-03": "Isolate OCR failure, verify blocking, restore/retry and withdraw a failed source.",
    "SOURCE-01": "Verify project-scoped source/original/GCS access and inert source-instruction rendering.",
    "AGENT-01": "Analyze authorized reviewed Harbour packet through actual Agent Engine and Vertex Gemini.",
    "AGENT-02": "Check all code-point citations; manually score non-BMP and PDF evidence.",
    "AGENT-03": "Run live clean, ambiguous, injection, foreign-reference and oversized-packet experiments.",
    "AGENT-04": "Exercise isolated real provider failure, restore, and label offline malformed-output tests.",
    "TX-01": "Inspect reviewed approval, execute and inspect atomic Firestore task/result/audit.",
    "TX-02": "Race executions, replay keys and capture actual Firestore contention/retry evidence.",
    "TX-03": "Exercise stale versions, permissions and trusted clock changes before execution.",
    "QUEUE-01": "Deliver duplicates via actual Tasks and capture competing fenced worker publication.",
    "OUTBOX-01": "Interrupt queue boundary windows and observe scheduled recovery within a finite window.",
    "OUTBOX-02": "Interrupt leased worker and exhaust retries on isolated synthetic resources.",
    "STATE-01": "Redeploy/restart then compare persisted Firestore/GCS state after real sign-in.",
    "REVIEW-01": "Review future/current replacements and advance only the trusted synthetic clock.",
    "REVIEW-02": "Reject premature closure, close with reviewed UAT, then separately resolve findings.",
    "OBS-01": "Inspect correlated API/outbox/Tasks/worker/agent logs for sensitive data exclusion.",
    "OPS-01": "Inspect actual bounds, secrets, IAM, images and available usage/billing evidence.",
}


class Blocked(Exception):
    """A specifically identified prerequisite stopped an attempted case."""


class Failed(Exception):
    """An actual observed behavior violated its assertion."""


@dataclass
class CaseResult:
    case_id: str
    status: str = "not_run"
    started_at: str | None = None
    completed_at: str | None = None
    assertions: list[dict] = field(default_factory=list)
    observations: dict = field(default_factory=dict)
    reason: str | None = None

    def check(self, condition: bool, description: str):
        self.assertions.append({"assertion": description, "passed": bool(condition)})
        if not condition:
            raise Failed(description)


def secure_token(directory: Path | None, role: str) -> str:
    if directory is None:
        raise Blocked(f"Provide the separately authenticated {role} Identity Platform token in --token-dir.")
    candidate = directory / f"{role}.token"
    path = candidate.resolve()
    if not path.is_file() or candidate.is_symlink() or not path.is_relative_to(directory.resolve()):
        raise Blocked(f"Missing restricted {role}.token from actual verified email/password sign-in.")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Blocked("Token files must have owner-only permissions (0600).")
    if os.name == "nt":
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=False)
        if acl.returncode or re.search(
            r"(?:Everyone|BUILTIN\\Users|NT AUTHORITY\\Authenticated Users|S-1-1-0|S-1-5-11|S-1-5-32-545):",
            acl.stdout,
            flags=re.IGNORECASE,
        ):
            raise Blocked("Restrict Windows token file ACL to the operator, SYSTEM and administrators.")
    if path.stat().st_size > 16384:
        raise Blocked("Invalid token file size.")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token:
        raise Blocked("Invalid token file format; one actual Identity Platform token per file.")
    return token


def pdf_bytes(text: str, pages: int = 1) -> bytes:
    """Generate synthetic selectable-text PDF; extraction must still use Document AI."""
    lines = []
    for paragraph in text.splitlines():
        raw = paragraph.encode("ascii", errors="replace").decode("ascii")
        lines.extend(raw[index : index + 90] for index in range(0, max(1, len(raw)), 90))
    per_page = 48
    if len(lines) > per_page * pages:
        raise ValueError("Generated PDF cannot fit the complete selected synthetic text.")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    page_ids = []
    for index in range(pages):
        page_id, content_id = len(objects) + 1, len(objects) + 2
        page_ids.append(page_id)
        page_lines = lines[index * per_page : (index + 1) * per_page] or ["Synthetic continuation page"]
        escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in page_lines]
        commands = "BT /F1 9 Tf 40 750 Td 13 TL " + " ".join(f"({line}) Tj T*" for line in escaped) + " ET"
        stream = commands.encode("ascii")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{value} 0 R' for value in page_ids)}] /Count {pages} >>"
    ).encode()
    result, offsets = bytearray(b"%PDF-1.4\n% synthetic Handoff Guardian\n"), [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(
        (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    )
    return bytes(result)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward user authorization headers to another host.
        return None


class Runner:
    def __init__(self, args):
        self.args = args
        self.project_path = f"/api/v1/projects/{urllib.parse.quote(args.project_id or '', safe='')}"
        self.shared = {}
        self.opener = urllib.request.build_opener(NoRedirect())
        self._google_session = None

    def require(self, *names):
        missing = ["--" + name.replace("_", "-") for name in names if not getattr(self.args, name, None)]
        if missing:
            raise Blocked("Required deployed inputs: " + ", ".join(missing))

    def token(self, role="reviewer"):
        return secure_token(self.args.token_dir, role)

    def http(
        self,
        path,
        *,
        role="reviewer",
        method="GET",
        body=None,
        raw=None,
        headers=None,
        origin=None,
        response_limit=4 * 1024 * 1024,
    ):
        if origin is None:
            self.require("api_url")
        headers = dict(headers or {})
        if role is not None:
            headers["Authorization"] = "Bearer " + self.token(role)
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            (origin or self.args.api_url).rstrip("/") + path, data=raw, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=45)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Blocked("Deployed HTTPS endpoint could not be reached within its deadline.") from exc
        with response:
            data = response.read(response_limit + 1)
            if len(data) > response_limit:
                raise Failed("API response exceeded acceptance evidence bound.")
            try:
                value = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                value = None
            return response.status, value, data

    def google(self, url):
        self.require("google_cloud_project")
        if self._google_session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if isinstance(credentials, service_account.Credentials):
                raise Blocked("Operator service-account key files are forbidden; use approved ADC.")
            self._google_session = AuthorizedSession(credentials)
        response = self._google_session.get(url, timeout=45, allow_redirects=False)
        if response.status_code != 200:
            raise Blocked(
                f"Google inventory HTTP {response.status_code}; verify target, API and operator IAM."
            )
        return response.json()

    def workspace(self, role="reviewer"):
        self.require("project_id")
        status, value, _ = self.http(self.project_path + "/workspace", role=role)
        if status != 200:
            raise Blocked(f"The provisioned {role} workspace is unavailable (HTTP {status}).")
        return value

    def actual_agent(self):
        self.require("google_cloud_project", "region", "agent_resource")
        project = self.google(
            f"https://cloudresourcemanager.googleapis.com/v3/projects/{self.args.google_cloud_project}"
        )
        project_number = project["name"].split("/")[-1]
        if self.args.agent_resource.split("/")[1] not in {self.args.google_cloud_project, project_number}:
            raise Failed("Configured agent resource belongs to another actual project.")
        return self.google(
            f"https://{self.args.region}-aiplatform.googleapis.com/v1/{self.args.agent_resource}"
        )

    def source(self, source_id, role="reviewer"):
        status, value, _ = self.http(
            self.project_path + "/sources/" + urllib.parse.quote(source_id, safe=""), role=role
        )
        if status != 200:
            raise Blocked(f"Source lookup is unavailable (HTTP {status}).")
        return value

    def upload(
        self,
        content,
        metadata,
        *,
        key,
        filename="synthetic-harbour.pdf",
        mime="application/pdf",
        role="reviewer",
    ):
        boundary = "hg" + uuid.uuid4().hex
        raw = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
            + json.dumps(metadata).encode()
            + (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return self.http(
            self.project_path + "/sources",
            role=role,
            method="POST",
            raw=raw,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": key},
        )

    def deploy(self, case):
        self.require("google_cloud_project", "region", "agent_resource", "build_id")
        base = f"https://run.googleapis.com/v2/projects/{self.args.google_cloud_project}/locations/{self.args.region}/services"
        services = {name: self.google(base + "/hg-" + name) for name in ("web", "api", "worker")}
        accounts = []
        protected = {
            "APP_ENV": "prototype",
            "AI_PROVIDER": "vertex",
            "AGENT_RUNTIME": "vertex_agent_engine",
            "AUTH_MODE": "identity_platform",
            "STORAGE_BACKEND": "firestore",
            "FILE_STORAGE_BACKEND": "gcs",
            "QUEUE_BACKEND": "cloud_tasks",
            "ALLOW_LIVE_MODEL_CALLS": "true",
        }
        for name, service in services.items():
            template = service["template"]
            container = template["containers"][0]
            env = {item["name"]: item.get("value", "") for item in container.get("env", [])}
            accounts.append(template.get("serviceAccount"))
            case.check(bool(service.get("latestReadyRevision")), f"{name} has an actual ready revision")
            case.check("@sha256:" in container["image"], f"{name} image is pinned by digest")
            case.check(
                not any("EMULATOR" in key and value for key, value in env.items()),
                f"{name} has no emulator endpoint",
            )
            if name != "web":
                case.check(
                    all(env.get(key) == value for key, value in protected.items()),
                    f"{name} protected runtime settings match",
                )
            if name == "worker":
                case.check(template.get("maxInstanceRequestConcurrency") == 1, "Worker concurrency is one")
                case.check(
                    template.get("scaling", {}).get("maxInstanceCount") == 2,
                    "Worker maximum instances is two",
                )
            case.observations[name] = {
                "resource": service["name"],
                "revision": service["latestReadyRevision"],
                "image": container["image"],
                "service_account": template.get("serviceAccount"),
            }
        case.check(
            len(set(accounts)) == 3 and all(accounts), "Run services use three separate runtime identities"
        )
        policy = self.google(base + "/hg-worker:getIamPolicy")
        invokers = [
            member
            for binding in policy.get("bindings", [])
            if binding["role"] == "roles/run.invoker"
            for member in binding.get("members", [])
        ]
        case.check(
            not {"allUsers", "allAuthenticatedUsers"}.intersection(invokers),
            "Worker has no public IAM invoker binding",
        )
        case.check(
            not services["worker"].get("invokerIamDisabled", False), "Worker IAM authentication is enabled"
        )
        agent = self.actual_agent()
        case.check(
            agent.get("name", "").split("/")[-1] == self.args.agent_resource.split("/")[-1],
            "Configured Agent Engine resource actually exists in the selected project",
        )
        build = self.google(
            f"https://cloudbuild.googleapis.com/v1/projects/{self.args.google_cloud_project}/builds/{self.args.build_id}"
        )
        case.check(build.get("status") == "SUCCESS", "Recorded actual Cloud Build completed successfully")
        built_digests = {item.get("digest") for item in build.get("results", {}).get("images", [])}
        case.check(
            all(
                service["template"]["containers"][0]["image"].split("@")[-1] in built_digests
                for service in services.values()
            ),
            "Deployed image digests appear in the actual successful build",
        )
        case.observations.update({"agent_resource": agent["name"], "build_id": build["id"]})

    def auth01(self, case):
        workspace = self.workspace()
        refreshed = self.workspace()
        case.check(
            workspace["actor_role"] == "reviewer",
            "API resolves real signed-in reviewer membership server-side",
        )
        case.check(
            refreshed["project"]["id"] == workspace["project"]["id"],
            "Repeated protected request retains project agreement",
        )
        case.observations["project_id"] = workspace["project"]["id"]
        raise Blocked(
            "API checks complete; observe email/password login, refresh and UI identity agreement."
        )

    def auth02(self, case):
        self.require("project_id")
        path = self.project_path + "/workspace"
        for label, role, headers in [
            ("missing", None, {}),
            ("demo header", None, {"X-Demo-Actor-Id": "usr_reviewer"}),
            ("invalid", None, {"Authorization": "Bearer deliberately.invalid.token"}),
        ]:
            status, _, _ = self.http(path, role=role, headers=headers)
            case.check(status == 401, f"{label} credentials return 401 without granting project access")
        for role in ("expired", "wrong-project"):
            status, _, _ = self.http(path, role=role)
            case.check(status == 401, f"Actual {role} user token returns 401")

    def auth03(self, case):
        workspace = self.workspace("viewer")
        case.check(workspace["actor_role"] == "viewer", "Viewer token is a real provisioned viewer")
        revision = workspace["project"]["evidence_revision"]
        metadata = {
            "document_id": "acceptance-viewer-denied",
            "version_number": 1,
            "title": "Denied synthetic viewer upload",
            "source_kind": "other",
            "created_at": timestamp(),
            "effective_at": None,
            "supersedes_source_version_id": None,
            "expected_evidence_revision": revision,
        }
        status, _, _ = self.upload(
            b"Denied synthetic upload",
            metadata,
            key="viewer-denied-" + uuid.uuid4().hex,
            filename="denied.txt",
            mime="text/plain",
            role="viewer",
        )
        case.check(status == 403, "Viewer upload is rejected before persistence")
        for suffix, body in [
            (
                "/analyses",
                {
                    "evaluation_as_of": workspace["project"]["business_clock"]["as_of"],
                    "expected_evidence_revision": revision,
                },
            ),
            ("/sources/acceptance-missing/retry-ingestion", {"expected_evidence_revision": revision}),
            (
                "/sources/acceptance-missing/withdraw-failed",
                {"expected_evidence_revision": revision, "reason": "Denied"},
            ),
        ]:
            status, _, _ = self.http(
                self.project_path + suffix,
                role="viewer",
                method="POST",
                body=body,
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            case.check(status == 403, f"Viewer {suffix.rsplit('/', 1)[-1]} returns 403")
        for path in [self.project_path + "/workspace", "/api/v1/projects/acceptance-unknown/workspace"]:
            status, _, _ = self.http(path, role="nonmember")
            case.check(status == 404, "Authenticated nonmember receives nondisclosing 404")
        case.check(
            self.workspace()["project"]["evidence_revision"] == revision,
            "Denied viewer writes leave evidence revision unchanged",
        )
        raise Blocked(
            "Viewer/nonmember probes complete; prepare actions to test review, execute and revocation."
        )

    def worker(self, case):
        self.require("worker_url")
        for role in (None, "reviewer", "nonmember"):
            status, _, _ = self.http(
                "/internal/recover", origin=self.args.worker_url, role=role, method="POST", body={}
            )
            case.check(
                status in (401, 403), "Worker rejects missing/user/unapproved identity before work access"
            )
        raise Blocked(
            "Negative probes complete; capture actual Tasks OIDC delivery with configured identity/audience."
        )

    def pdf01(self, case):
        root = Path(__file__).resolve().parents[1]
        workspace = self.workspace()
        self.assert_synthetic(workspace)
        selected = self.args.pdf_source_id
        if selected:
            source = self.source(selected)
        else:
            text = (root / "fixtures/inputs/sources/prj_harbour_portal/sv_hp_sow_001.md").read_text(
                encoding="utf-8"
            )
            content = pdf_bytes(text)
            metadata = {
                "document_id": "doc_hp_sow",
                "version_number": self.args.pdf_version,
                "title": "Synthetic Harbour SOW PDF",
                "source_kind": "sow",
                "created_at": "2026-10-20T09:00:00Z",
                "effective_at": None,
                "supersedes_source_version_id": None,
                "expected_evidence_revision": workspace["project"]["evidence_revision"],
            }
            key = self.args.upload_key or "acceptance-pdf-" + uuid.uuid4().hex
            status, source, _ = self.upload(content, metadata, key=key)
            case.check(status == 202, "Real PDF upload returns 202")
            status, replay, _ = self.upload(content, metadata, key=key)
            case.check(
                status == 202 and replay["id"] == source["id"],
                "Identical PDF upload replays the existing source",
            )
            status, _, _ = self.upload(content + b"\n% changed synthetic bytes", metadata, key=key)
            case.check(status == 409, "Changed upload bytes under the same key conflict")
            until = time.monotonic() + self.args.observation_seconds
            while source["ingestion_status"] in ("queued", "processing") and time.monotonic() < until:
                time.sleep(2)
                source = self.source(source["id"])
        case.observations["source_id"] = source["id"]
        case.observations["ingestion_status"] = source["ingestion_status"]
        if source["ingestion_status"] in ("queued", "processing"):
            raise Failed("PDF ingestion did not complete within declared finite observation window.")
        case.check(source["ingestion_status"] == "ready", "Real ingestion finishes ready")
        status, _, original = self.http(
            self.project_path + "/sources/" + urllib.parse.quote(source["id"], safe="") + "/original",
            response_limit=10 * 1024 * 1024,
        )
        case.check(
            status == 200 and hashlib.sha256(original).hexdigest() == source["content_sha256"],
            "Authorized original route reads actual bytes matching the immutable raw hash",
        )
        case.check(
            source["extraction_method"] == "document_ai_ocr", "PDF canonical text came from Document AI"
        )
        case.check(bool(source.get("processor_version")), "Exact processor version is recorded")
        text = source["canonical_text"]
        case.check(
            hashlib.sha256(text.encode()).hexdigest() == source["canonical_text_sha256"],
            "Derived canonical hash matches current text",
        )
        case.check(
            bool(source["page_spans"])
            and all(0 <= p["start_char"] <= p["end_char"] <= len(text) for p in source["page_spans"]),
            "PDF page spans address derived Unicode code points",
        )
        case.observations.update(
            {
                "canonical_hash": source["canonical_text_sha256"],
                "content_hash": source["content_sha256"],
                "processor_version": source.get("processor_version"),
            }
        )
        if source["authority"] != "accepted_baseline":
            raise Blocked(
                "PDF extracted; inspect text, review baseline authority, then rerun with --pdf-source-id."
            )
        case.check(
            bool(source.get("authority_confirmed_by")), "Reviewer explicitly assigned source authority"
        )
        self.shared["pdf_source_id"] = source["id"]

    def assert_synthetic(self, workspace):
        self.require("google_cloud_project", "project_id")
        db = cloud_client(self.args.google_cloud_project, self.args.database)
        project = db.collection("projects").document(self.args.project_id).get().to_dict()
        if (
            not project
            or project.get("_synthetic") is not True
            or workspace["project"]["business_clock"]["mode"] != "synthetic"
        ):
            raise Blocked(
                "Mutating acceptance requires an operator-provisioned explicitly synthetic project."
            )

    def agent01(self, case):
        self.require("agent_resource", "region")
        workspace = self.workspace()
        self.assert_synthetic(workspace)
        pdf_id = self.args.pdf_source_id or self.shared.get("pdf_source_id")
        if not pdf_id:
            raise Blocked("Supply the actual reviewed Document AI PDF --pdf-source-id.")
        pdf = self.source(pdf_id)
        case.check(
            pdf["extraction_method"] == "document_ai_ocr" and pdf["authority"] == "accepted_baseline",
            "The actual PDF-derived reviewed baseline is present",
        )
        if self.args.analysis_run_id:
            status, run, _ = self.http(self.project_path + "/analyses/" + self.args.analysis_run_id)
            case.check(status == 200, "Existing live analysis is retrievable")
        else:
            body = {
                "evaluation_as_of": workspace["project"]["business_clock"]["as_of"],
                "expected_evidence_revision": workspace["project"]["evidence_revision"],
            }
            key = self.args.analysis_key or "acceptance-agent-" + uuid.uuid4().hex
            status, run, _ = self.http(
                self.project_path + "/analyses", method="POST", body=body, headers={"Idempotency-Key": key}
            )
            case.check(status == 202, "Actual analysis is durably queued")
            status, replay, _ = self.http(
                self.project_path + "/analyses", method="POST", body=body, headers={"Idempotency-Key": key}
            )
            case.check(status == 202 and replay["id"] == run["id"], "Analysis replay returns existing run")
            until = time.monotonic() + self.args.observation_seconds
            while run["status"] in ("queued", "running") and time.monotonic() < until:
                time.sleep(2)
                status, run, _ = self.http(self.project_path + "/analyses/" + run["id"])
                case.check(status == 200, "Queued live analysis remains pollable")
        case.observations.update(
            {
                "analysis_run_id": run["id"],
                "status": run["status"],
                "provider": run.get("provider"),
                "model": run.get("model"),
                "prompt_version": run.get("prompt_version"),
                "packet_fingerprint": run.get("packet_fingerprint"),
                "usage": run.get("usage", "unknown"),
            }
        )
        case.check(run["status"] == "succeeded", "Actual live analysis succeeds within observation window")
        case.check(
            run["provider"] == "vertex" and bool(run.get("model")),
            "Analysis records Vertex provider and actual model",
        )
        case.check(pdf_id in run["source_version_ids"], "Analysis includes actual PDF-derived source")
        case.check(
            all(
                finding["kind"] in {"scope_mismatch", "acceptance_gap", "dependency_conflict"}
                for finding in run["findings"]
            ),
            "All findings use the three permitted kinds",
        )
        agent = self.actual_agent()
        case.observations["agent_resource"] = agent["name"]
        db = cloud_client(self.args.google_cloud_project, self.args.database)
        stored = (
            db.collection("projects")
            .document(self.args.project_id)
            .collection("analyses")
            .document(run["id"])
            .get()
            .to_dict()
        )
        provenance = stored.get("_provider_provenance", {}) if stored else {}
        case.check(
            bool(provenance.get("agent_resource"))
            and provenance["agent_resource"].split("/")[-1] == self.args.agent_resource.split("/")[-1],
            "Actual persisted provider provenance identifies the configured managed agent",
        )
        case.observations.update(
            {
                "packet_object_sha256": stored.get("_snapshot", {}).get("sha256"),
                "packet_fingerprint": provenance.get("fingerprint", "unavailable"),
                "usage": provenance.get("usage", "unknown"),
                "model_calls": provenance.get("model_calls", "unknown"),
            }
        )
        self.shared["run"] = run

    def agent02(self, case):
        run = self.shared.get("run")
        if run is None:
            self.require("analysis_run_id")
            status, run, _ = self.http(self.project_path + "/analyses/" + self.args.analysis_run_id)
            case.check(status == 200 and run["status"] == "succeeded", "Actual succeeded run is retrievable")
        sources = {sid: self.source(sid) for sid in run["source_version_ids"]}
        count = 0
        for item in run["findings"] + run.get("candidate_commitments", []):
            for span in item["evidence"]:
                source = sources.get(span["source_version_id"])
                case.check(source is not None, "Citation source belongs to actual captured source set")
                text = source["canonical_text"]
                case.check(
                    0 <= span["start_char"] < span["end_char"] <= len(text)
                    and text[span["start_char"] : span["end_char"]] == span["quote"],
                    "Persisted citation exactly slices immutable canonical Unicode code points",
                )
                count += 1
        case.observations.update({"analysis_run_id": run["id"], "verified_spans": count})
        raise Blocked(
            "Span checks complete; manually score live semantics and exercise non-BMP plus PDF citations."
        )

    def tx01(self, case):
        self.require("approved_action_id", "google_cloud_project")
        before = self.workspace()
        self.assert_synthetic(before)
        action = next(
            (item for item in before["action_proposals"] if item["id"] == self.args.approved_action_id), None
        )
        if not action or action["status"] != "approved":
            raise Blocked(
                "Select an existing current action explicitly approved by the signed-in reviewer in the UI."
            )
        case.check(
            not any(task["action_proposal_id"] == action["id"] for task in before["tasks"]),
            "Proposal and approval have created zero tasks",
        )
        case.check(
            action["binding"]["base_board_revision"] == before["project"]["board_revision"],
            "Approval retained the current board revision without changing it",
        )
        path = (
            self.project_path + "/action-proposals/" + urllib.parse.quote(action["id"], safe="") + "/execute"
        )
        body, key = {"binding": action["binding"]}, "acceptance-execute-" + uuid.uuid4().hex
        status, executed, _ = self.http(path, method="POST", body=body, headers={"Idempotency-Key": key})
        case.check(status == 201, "First execution returns 201 after actual commit")
        after = self.workspace()
        task_id = executed["task"]["id"]
        case.check(len(after["tasks"]) == len(before["tasks"]) + 1, "Board contains exactly one new task")
        case.check(
            after["project"]["board_revision"] == before["project"]["board_revision"] + 1,
            "Board revision increments once",
        )
        case.check(
            executed["task"]["status"] == "open" and executed["task"]["version"] == 1,
            "New task is open version one",
        )
        replay_keys = (key, "acceptance-replay-" + uuid.uuid4().hex)
        for replay_key in replay_keys:
            status, replay, _ = self.http(
                path, method="POST", body=body, headers={"Idempotency-Key": replay_key}
            )
            case.check(
                status == 200 and replay["task"]["id"] == task_id and replay["replayed"],
                "Same and fresh execution keys replay one committed task",
            )
        db = cloud_client(self.args.google_cloud_project, self.args.database)
        ref = db.collection("projects").document(self.args.project_id)
        task = ref.collection("tasks").document(task_id).get().to_dict()
        stored = ref.collection("action_proposals").document(action["id"]).get().to_dict()
        case.check(
            task is not None and stored["task_id"] == task_id and stored["status"] == "executed",
            "Actual Firestore action and task agree",
        )
        for replay_key in replay_keys:
            receipt_id = hashlib.sha256(
                json.dumps(
                    [stored["_executed_by"], "execute", replay_key], ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            receipt = ref.collection("receipts").document(receipt_id).get().to_dict()
            request_hash = hashlib.sha256(
                json.dumps(
                    {"action_id": action["id"], "binding": action["binding"]},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            case.check(
                receipt is not None
                and receipt["target_id"] == task_id
                and receipt["request_hash"] == request_hash,
                "Actual Firestore execution receipt binds this exact request to one task",
            )
        case.check(
            any(
                event["event_type"] in {"action_executed", "task_created", "blocker_task_created"}
                and event["target_id"] in {action["id"], task_id}
                for event in after["audit_events"]
            ),
            "Actual execution audit event is visible",
        )
        case.check(
            self.workspace()["project"]["board_revision"] == after["project"]["board_revision"],
            "Replays leave board revision unchanged",
        )
        case.observations.update(
            {
                "action_id": action["id"],
                "task_id": task_id,
                "board_revision": after["project"]["board_revision"],
                "payload_hash": action["binding"]["payload_hash"],
            }
        )

    def tx02(self, case):
        self.require("concurrency_action_id")
        workspace = self.workspace()
        self.assert_synthetic(workspace)
        action = next(
            (item for item in workspace["action_proposals"] if item["id"] == self.args.concurrency_action_id),
            None,
        )
        if not action or action["status"] != "approved":
            raise Blocked("Provide a separate fresh reviewer-approved action for the execution race.")
        path = (
            self.project_path + "/action-proposals/" + urllib.parse.quote(action["id"], safe="") + "/execute"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.http,
                    path,
                    method="POST",
                    body={"binding": action["binding"]},
                    headers={"Idempotency-Key": "acceptance-race-" + uuid.uuid4().hex},
                )
                for _ in range(2)
            ]
            results = [future.result() for future in futures]
        case.check(
            all(status in (200, 201) for status, _, _ in results),
            "Both real concurrent executions return success/replay",
        )
        case.check(
            len({value["task"]["id"] for _, value, _ in results}) == 1,
            "Concurrent executions identify one task",
        )
        after = self.workspace()
        case.check(
            after["project"]["board_revision"] == workspace["project"]["board_revision"] + 1,
            "Race increments the board once",
        )
        case.observations["task_id"] = results[0][1]["task"]["id"]
        raise Blocked(
            "Concurrent execution succeeded; capture actual Firestore contention/retry evidence for TX-02."
        )

    def run(self, case):
        methods = {
            "DEPLOY-01": self.deploy,
            "AUTH-01": self.auth01,
            "AUTH-02": self.auth02,
            "AUTH-03": self.auth03,
            "WORKER-01": self.worker,
            "PDF-01": self.pdf01,
            "AGENT-01": self.agent01,
            "AGENT-02": self.agent02,
            "TX-01": self.tx01,
            "TX-02": self.tx02,
        }
        if case.case_id not in methods:
            raise Blocked(
                "Requires privileged isolated live experiment or rendered/manual observation: "
                + CASES[case.case_id]
            )
        methods[case.case_id](case)


def https_origin(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError(
            "Supply the actual HTTPS origin without credentials, query or fragment."
        )
    if parts.path not in ("", "/") or parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise argparse.ArgumentTypeError("Cloud acceptance requires a deployed HTTPS origin, without path.")
    return value.rstrip("/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", choices=CASES, action="append", dest="cases")
    selection.add_argument("--all", action="store_true")
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="Enable actual cloud calls and bounded synthetic upload/model/task effects.",
    )
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--api-url", type=https_origin)
    parser.add_argument("--web-url", type=https_origin)
    parser.add_argument("--worker-url", type=https_origin)
    parser.add_argument("--google-cloud-project", type=identifier)
    parser.add_argument("--project-id", type=identifier)
    parser.add_argument("--database", default="(default)")
    parser.add_argument("--region")
    parser.add_argument("--agent-resource")
    parser.add_argument("--build-id")
    parser.add_argument(
        "--token-dir",
        type=Path,
        help="Restricted external directory: reviewer/viewer/nonmember/expired/wrong-project.token files.",
    )
    parser.add_argument("--pdf-source-id", type=identifier)
    parser.add_argument("--pdf-version", type=int, default=1)
    parser.add_argument("--upload-key", help="Reuse this key after interrupted upload; no bearer tokens.")
    parser.add_argument("--analysis-run-id", type=identifier)
    parser.add_argument("--analysis-key")
    parser.add_argument("--approved-action-id", type=identifier)
    parser.add_argument("--concurrency-action-id", type=identifier)
    parser.add_argument("--observation-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.observation_seconds <= 900:
        parser.error("Observation window must be 1-900 seconds.")
    root = Path(__file__).resolve().parents[1]
    if args.token_dir and args.token_dir.resolve().is_relative_to(root):
        parser.error("Keep temporary restricted user tokens outside the repository.")
    if args.agent_resource and (
        not args.google_cloud_project
        or not args.region
        or not re.fullmatch(
            rf"projects/(?:{re.escape(args.google_cloud_project)}|[0-9]+)/locations/"
            rf"{re.escape(args.region)}/reasoningEngines/[A-Za-z0-9_-]+",
            args.agent_resource,
        )
    ):
        parser.error("Agent resource must belong to the explicitly selected project/region.")
    git = shutil.which("git")
    revision = (
        subprocess.run([git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
        if git
        else None
    )
    report = {
        "schema_version": "1.0",
        "environment": "google_cloud",
        "started_at": timestamp(),
        "source_revision": revision.stdout.strip()
        if revision and revision.returncode == 0
        else "unavailable",
        "live_authorized": args.allow_live,
        "google_cloud_project": args.google_cloud_project,
        "domain_project_id": args.project_id,
        "observation_seconds": args.observation_seconds,
        "cases": [],
    }
    runner = Runner(args)
    for case_id in list(CASES) if args.all else args.cases:
        case = CaseResult(case_id)
        if not args.allow_live:
            case.reason = "Live disabled; use --allow-live after authorized deployment and real sign-in."
        else:
            case.started_at = timestamp()
            try:
                runner.run(case)
                case.status = "passed"
            except Blocked as exc:
                case.status, case.reason = "blocked", str(exc)
            except Failed as exc:
                case.status, case.reason = "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 -- sanitize remote/provider failures at the CLI boundary
                case.status = "blocked"
                case.reason = "Live incomplete; verify ADC, IAM, resource settings and response schema."
                case.observations["error_type"] = type(exc).__name__
            case.completed_at = timestamp()
        report["cases"].append(vars(case))
        print(json.dumps({"case": case_id, "status": case.status, "reason": case.reason}), flush=True)
        report["completed_at"] = timestamp()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(case["status"] == "passed" for case in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
