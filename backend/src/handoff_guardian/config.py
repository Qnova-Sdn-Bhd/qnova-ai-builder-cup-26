"""Production-only settings. Tests inject collaborators; no runtime demo switch exists."""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    project: str
    location: str
    firebase_project: str
    bucket: str
    processor_version: str
    docai_location: str
    agent_resource: str
    model: str
    worker_url: str
    task_invoker: str
    scheduler_invoker: str
    queue_location: str
    queue: str
    cors_origins: tuple[str, ...]
    database: str = "(default)"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_source_pages: int = 15
    max_analysis_sources: int = 30
    max_analysis_chars: int = 120000
    job_max_attempts: int = 3
    job_lease_seconds: int = 360
    document_ai_timeout: int = 120
    model_timeout: int = 60

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        protected = {
            "APP_ENV": "prototype",
            "AI_PROVIDER": "vertex",
            "AGENT_RUNTIME": "vertex_agent_engine",
            "AUTH_MODE": "identity_platform",
            "STORAGE_BACKEND": "firestore",
            "FILE_STORAGE_BACKEND": "gcs",
            "QUEUE_BACKEND": "cloud_tasks",
            "TASK_BOARD_BACKEND": "firestore",
            "ALLOW_LIVE_MODEL_CALLS": "true",
        }
        invalid = [key for key, value in protected.items() if e.get(key) != value]
        policy = {
            "MAX_UPLOAD_BYTES": 10485760,
            "MAX_SOURCE_PAGES": 15,
            "MAX_ANALYSIS_SOURCES": 30,
            "MAX_ANALYSIS_CHARS": 120000,
            "MODEL_TIMEOUT_SECONDS": 60,
            "MODEL_MAX_RETRIES": 2,
            "MODEL_MAX_CALLS": 3,
            "DOCUMENT_AI_TIMEOUT_SECONDS": 120,
            "TASK_DISPATCH_DEADLINE_SECONDS": 300,
            "WORKER_TIMEOUT_SECONDS": 300,
            "JOB_LEASE_SECONDS": 360,
            "JOB_MAX_ATTEMPTS": 3,
        }
        invalid += [key for key, value in policy.items() if e.get(key, str(value)) != str(value)]
        forbidden = [
            key
            for key in e
            if (
                "EMULATOR" in key
                or key
                in {
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "FIRESTORE_HOST",
                    "STORAGE_EMULATOR_HOST",
                    "DEMO_ACTOR_ID",
                    "DATABASE_URL",
                }
            )
            and e[key]
        ]
        names = [
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "FIREBASE_PROJECT_ID",
            "GCS_BUCKET",
            "DOCUMENT_AI_LOCATION",
            "DOCUMENT_AI_PROCESSOR_ID",
            "DOCUMENT_AI_PROCESSOR_VERSION",
            "VERTEX_AGENT_ENGINE_RESOURCE_NAME",
            "GEMINI_MODEL",
            "WORKER_SERVICE_URL",
            "TASK_INVOKER_SERVICE_ACCOUNT",
            "SCHEDULER_INVOKER_SERVICE_ACCOUNT",
            "CLOUD_TASKS_LOCATION",
            "CLOUD_TASKS_QUEUE",
            "CORS_ALLOW_ORIGINS",
        ]
        missing = [key for key in names if not e.get(key, "").strip()]
        if invalid or forbidden or missing:
            fields = ",".join(sorted(set(invalid + forbidden + missing)))
            raise ValueError(f"Invalid Google runtime configuration: {fields}")
        project, location = e["GOOGLE_CLOUD_PROJECT"], e["GOOGLE_CLOUD_LOCATION"]
        resource = e["VERTEX_AGENT_ENGINE_RESOURCE_NAME"]
        if not re.fullmatch(r"projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+", resource):
            raise ValueError("Invalid VERTEX_AGENT_ENGINE_RESOURCE_NAME")
        worker_url = e["WORKER_SERVICE_URL"].rstrip("/")
        origins = tuple(item.strip().rstrip("/") for item in e["CORS_ALLOW_ORIGINS"].split(","))
        for url in (worker_url, *origins):
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Worker and CORS settings must be exact HTTPS origins")
        for name in ("TASK_INVOKER_SERVICE_ACCOUNT", "SCHEDULER_INVOKER_SERVICE_ACCOUNT"):
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com", e[name]):
                raise ValueError(f"Invalid {name}")
        processor = (
            f"projects/{project}/locations/{e['DOCUMENT_AI_LOCATION']}/processors/"
            f"{e['DOCUMENT_AI_PROCESSOR_ID']}/processorVersions/{e['DOCUMENT_AI_PROCESSOR_VERSION']}"
        )
        return cls(
            project,
            location,
            e["FIREBASE_PROJECT_ID"],
            e["GCS_BUCKET"],
            processor,
            e["DOCUMENT_AI_LOCATION"],
            resource,
            e["GEMINI_MODEL"],
            worker_url,
            e["TASK_INVOKER_SERVICE_ACCOUNT"],
            e["SCHEDULER_INVOKER_SERVICE_ACCOUNT"],
            e["CLOUD_TASKS_LOCATION"],
            e["CLOUD_TASKS_QUEUE"],
            origins,
            e.get("FIRESTORE_DATABASE", "(default)"),
        )
