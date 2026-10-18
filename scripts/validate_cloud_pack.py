#!/usr/bin/env python3
"""Check cloud starter-pack artifacts with the standard library, without network.

This validates specification/configuration consistency, not deployed resources,
IAM, application functionality, Google authentication, inference or live tests.
Only .env.example is read; this script never reads the operator's real .env.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys
from urllib.parse import unquote, urlsplit


PROTECTED_DEFAULTS = {
    "APP_ENV": "prototype",
    "AI_PROVIDER": "vertex",
    "AGENT_RUNTIME": "vertex_agent_engine",
    "AUTH_MODE": "identity_platform",
    "STORAGE_BACKEND": "firestore",
    "FILE_STORAGE_BACKEND": "gcs",
    "QUEUE_BACKEND": "cloud_tasks",
    "ALLOW_LIVE_MODEL_CALLS": "true",
    "MAX_UPLOAD_BYTES": "10485760",
    "MAX_SOURCE_PAGES": "15",
}

REQUIRED_SETTINGS = {
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GEMINI_MODEL",
    "VERTEX_AGENT_ENGINE_RESOURCE_NAME", "FIRESTORE_DATABASE", "GCS_BUCKET",
    "DOCUMENT_AI_LOCATION", "DOCUMENT_AI_PROCESSOR_ID", "DOCUMENT_AI_PROCESSOR_VERSION",
    "CLOUD_TASKS_LOCATION", "CLOUD_TASKS_QUEUE", "WORKER_SERVICE_URL",
    "TASK_INVOKER_SERVICE_ACCOUNT", "SCHEDULER_INVOKER_SERVICE_ACCOUNT",
    "FIREBASE_PROJECT_ID", "FIREBASE_AUTH_DOMAIN", "CORS_ALLOW_ORIGINS",
    "NEXT_PUBLIC_API_BASE_URL", "NEXT_PUBLIC_FIREBASE_API_KEY",
    "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN", "NEXT_PUBLIC_FIREBASE_PROJECT_ID",
    "NEXT_PUBLIC_FIREBASE_APP_ID", "MODEL_TIMEOUT_SECONDS", "MODEL_MAX_RETRIES",
    "MODEL_MAX_CALLS", "DOCUMENT_AI_TIMEOUT_SECONDS", "TASK_DISPATCH_DEADLINE_SECONDS",
    "WORKER_TIMEOUT_SECONDS", "JOB_LEASE_SECONDS", "JOB_MAX_ATTEMPTS",
}

FORBIDDEN_SETTINGS = {
    "DEMO_BOOTSTRAP_ACTOR_ID", "DATABASE_URL", "LOCAL_FILE_ROOT",
    "FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST",
    "STORAGE_EMULATOR_HOST", "CLOUD_TASKS_EMULATOR_HOST",
}

REQUIRED_SERVICE_APIS = {
    "run.googleapis.com", "identitytoolkit.googleapis.com",
    "firestore.googleapis.com", "storage.googleapis.com",
    "documentai.googleapis.com", "aiplatform.googleapis.com",
    "cloudtasks.googleapis.com", "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com", "logging.googleapis.com",
    "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
}

REQUIRED_PHASES = (
    "phase-00-cloud-foundation.md", "phase-01-identity-and-evidence.md",
    "phase-02-agent-and-analysis.md", "phase-03-review-and-actions.md",
    "phase-04-cloud-evaluation.md", "phase-05-deployment-and-demo.md",
    "phase-06-search-and-analytics.md",
)

REQUIRED_PATHS = (
    ".env.example", "AGENTS.md", "PLANS.md", "README.md", "CODEX_START_PROMPT.md",
    "docs/ARCHITECTURE.md", "docs/DOMAIN_AND_API.md", "docs/SECURITY_AND_ACTIONS.md",
    "docs/GCP_SETUP.md", "docs/DEPLOYMENT_PLAN.md", "docs/REVISION_NOTES.md",
    "docs/PRODUCT_BRIEF.md", "docs/UX_SPEC.md", "docs/DEMO_SCRIPT.md",
    "docs/EVALUATION.md", "docs/CLOUD_ACCEPTANCE.md", "fixtures/README.md",
    "fixtures/inputs/manifest.json", "contracts/fixtures.schema.json",
    "contracts/openapi.yaml", "scripts/validate_fixtures.py",
) + tuple("harness/build/" + name for name in REQUIRED_PHASES)

REQUIRED_CASES = {
    "DEPLOY-01", "AUTH-01", "AUTH-02", "AUTH-03", "WORKER-01",
    "PDF-01", "PDF-02", "PDF-03", "SOURCE-01",
    "AGENT-01", "AGENT-02", "AGENT-03", "AGENT-04",
    "TX-01", "TX-02", "TX-03", "QUEUE-01", "OUTBOX-01", "OUTBOX-02",
    "STATE-01", "REVIEW-01", "REVIEW-02", "OBS-01", "OPS-01",
}


class PackCheck:
    def __init__(self, root):
        self.root = root.resolve()
        self.errors = []
        self.checks = 0

    def check(self, condition, message):
        self.checks += 1
        if not condition:
            self.errors.append(message)
        return bool(condition)

    def read(self, relative):
        path = (self.root / relative).resolve()
        if not self.check(path.is_relative_to(self.root), f"Artifact escapes pack root: {relative}"):
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.errors.append(f"Cannot read {relative}: {type(exc).__name__}")
            return ""


def parse_env(text, check):
    """Read simple dotenv assignments as data; never expand or execute values."""
    settings = {}
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=(.*)", line)
        if not check.check(bool(match), f"Unsupported .env.example syntax at line {line_number}"):
            continue
        key, value = match.groups()
        if not check.check(key not in settings, f"Duplicate .env.example setting: {key}"):
            continue
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            check.errors.append(f"Invalid quoting in .env.example setting: {key}")
            continue
        if not check.check(len(parts) <= 1, f"Unquoted whitespace in .env.example setting: {key}"):
            continue
        settings[key] = parts[0] if parts else ""
    return settings


def check_settings(settings, check):
    for key, value in PROTECTED_DEFAULTS.items():
        check.check(settings.get(key) == value, f"Protected template default must be {key}={value}")
    for key in sorted(REQUIRED_SETTINGS):
        check.check(key in settings, f"Missing resource/policy template setting: {key}")
    for key in sorted(FORBIDDEN_SETTINGS):
        check.check(key not in settings, f"Local/demo/emulator setting must not be in deployed template: {key}")
    for key in settings:
        check.check("EMULATOR" not in key, f"Emulator setting must remain test-only: {key}")
    if "TASK_BOARD_BACKEND" in settings:
        check.check(settings["TASK_BOARD_BACKEND"] == "firestore", "TASK_BOARD_BACKEND must be firestore")
    for key in ("AGENT_SEARCH_ENABLED", "BIGQUERY_ENABLED"):
        if key in settings:
            check.check(settings[key] == "false", f"Optional phase-06 extension must default off: {key}")

    integers = {}
    numeric_keys = (
        "MODEL_TIMEOUT_SECONDS", "MODEL_MAX_RETRIES", "MODEL_MAX_CALLS",
        "DOCUMENT_AI_TIMEOUT_SECONDS", "TASK_DISPATCH_DEADLINE_SECONDS",
        "WORKER_TIMEOUT_SECONDS", "JOB_LEASE_SECONDS", "JOB_MAX_ATTEMPTS",
    )
    for key in numeric_keys:
        value = settings.get(key, "")
        if check.check(bool(re.fullmatch(r"[0-9]+", value)), f"Expected integer policy setting: {key}"):
            integers[key] = int(value)
            check.check(int(value) >= (0 if key == "MODEL_MAX_RETRIES" else 1), f"Invalid lower policy bound: {key}")
    if set(numeric_keys) <= integers.keys():
        check.check(integers["MODEL_MAX_CALLS"] <= 3, "Per-agent invocation cap exceeds 3 model requests")
        check.check(integers["MODEL_MAX_RETRIES"] < integers["MODEL_MAX_CALLS"], "Repair/retry cap must fit total per-invocation call cap")
        check.check(integers["JOB_MAX_ATTEMPTS"] <= 3, "Logical job cap exceeds 3 attempts")
        check.check(integers["WORKER_TIMEOUT_SECONDS"] <= integers["TASK_DISPATCH_DEADLINE_SECONDS"], "Worker deadline exceeds Cloud Tasks dispatch deadline")
        check.check(integers["JOB_LEASE_SECONDS"] > integers["TASK_DISPATCH_DEADLINE_SECONDS"], "Job lease must exceed dispatch deadline")
        for key in ("MODEL_TIMEOUT_SECONDS", "DOCUMENT_AI_TIMEOUT_SECONDS"):
            check.check(integers[key] < integers["WORKER_TIMEOUT_SECONDS"], f"{key} must leave time before worker deadline")

    for key in ("NEXT_PUBLIC_API_BASE_URL", "WORKER_SERVICE_URL", "CORS_ALLOW_ORIGINS"):
        for value in settings.get(key, "").split(","):
            value = value.strip()
            if not value:
                continue  # The artifact template intentionally has unresolved resources.
            parsed = urlsplit(value)
            check.check(
                parsed.scheme == "https" and bool(parsed.hostname)
                and parsed.hostname not in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
                and parsed.username is None and parsed.password is None,
                f"Deployed template URL must be HTTPS, non-loopback and credential-free: {key}",
            )
    return integers


def documentation_paths(root):
    paths = set(root.glob("*.md"))
    for directory in ("docs", "harness"):
        paths.update((root / directory).rglob("*.md"))
    paths.add(root / "fixtures/README.md")
    return sorted(path for path in paths if path.is_file())


def check_local_links(check):
    """Check existing-file links, not web URLs, anchors or future code in fences."""
    checked = 0
    for path in documentation_paths(check.root):
        relative = path.relative_to(check.root)
        body = check.read(relative)
        body = re.sub(r"^\s*(```|~~~)[^\n]*\n.*?^\s*\1\s*$", "", body, flags=re.M | re.S)
        body = re.sub(r"`[^`\n]*`", "", body)
        for match in re.finditer(r"\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+\"[^\"]*\")?\)", body):
            raw = match.group(1)
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (path.parent / unquote(parsed.path)).resolve()
            checked += 1
            check.check(target.is_relative_to(check.root), f"Local documentation link escapes pack: {relative} -> {raw}")
            check.check(target.exists(), f"Missing local documentation target: {relative} -> {raw}")
    return checked


def validate(root):
    check = PackCheck(root)
    for relative in REQUIRED_PATHS:
        path = (check.root / relative).resolve()
        check.check(path.is_relative_to(check.root) and path.is_file(), f"Missing required artifact: {relative}")
    settings = parse_env(check.read(".env.example"), check)
    numbers = check_settings(settings, check)

    inventory = check.read("docs/GCP_SETUP.md") + "\n" + check.read("docs/DEPLOYMENT_PLAN.md")
    found_services = set(re.findall(r"\b[a-z][a-z0-9-]*\.googleapis\.com\b", inventory))
    for service in sorted(REQUIRED_SERVICE_APIS):
        check.check(service in found_services, f"Required API absent from cloud service inventory: {service}")

    acceptance = check.read("docs/CLOUD_ACCEPTANCE.md")
    case_rows = re.findall(r"^\|\s*`([A-Z]+-[0-9]{2})`\s*\|", acceptance, flags=re.M)
    check.check(len(case_rows) == len(set(case_rows)), "Duplicate cloud acceptance case IDs")
    for case in sorted(REQUIRED_CASES):
        check.check(case in case_rows, f"Missing cloud acceptance requirement: {case}")
    links = check_local_links(check)

    return {
        "status": "failed" if check.errors else "passed",
        "validation_scope": "static_artifact_consistency_only",
        "checks": check.checks,
        "required_artifacts": len(REQUIRED_PATHS),
        "protected_defaults": PROTECTED_DEFAULTS,
        "required_google_service_apis": len(REQUIRED_SERVICE_APIS),
        "cloud_acceptance_cases_defined": len(case_rows),
        "local_documentation_links_checked": links,
        "unresolved_template_settings": sorted(key for key in REQUIRED_SETTINGS if not settings.get(key)),
        "maximum_model_requests_per_logical_analysis": numbers.get("MODEL_MAX_CALLS", 0) * numbers.get("JOB_MAX_ATTEMPTS", 0) or None,
        "fixture_integrity": "run_validate_fixtures_separately",
        "cloud_deployment": "not_run",
        "google_authentication": "not_run",
        "document_ai_pdf_ingestion": "not_run",
        "agent_engine_gemini_inference": "not_run",
        "firestore_queue_recovery_tests": "not_run",
        "live_accuracy_metrics": "not_measured",
        "errors": check.errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="Print a machine-readable artifact-check report")
    args = parser.parse_args()
    try:
        report = validate(args.root)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "status": "failed", "validation_scope": "static_artifact_consistency_only",
            "cloud_deployment": "not_run", "errors": [f"Artifact could not be checked: {type(exc).__name__}"],
        }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Cloud pack: {report['status'].upper()} (static artifacts only)")
        if "checks" in report:
            print(f"{report['checks']} checks; {report['cloud_acceptance_cases_defined']} cloud test requirements defined, none executed.")
        for error in report["errors"]:
            print(f"- {error}")
        print("Deployment, email/password login, Document AI, Agent Engine/Gemini and Firestore/queue tests: NOT RUN.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
