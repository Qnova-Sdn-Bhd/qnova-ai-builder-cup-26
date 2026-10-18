"""Google SDK boundaries. No model or persistence fallback is available."""

import asyncio
import copy
import io
import json
from collections.abc import Callable

from .contracts import canonical, digest
from .errors import AppError, require
from .ports import ProviderResponse


class FirestoreUnit:
    def __init__(self, client, transaction=None):
        self.client, self.transaction = client, transaction
        self.writes = []

    def get(self, path):
        snapshot = self.client.document(path).get(transaction=self.transaction, retry=None, timeout=30)
        return snapshot.to_dict() if snapshot.exists else None

    def list(self, path, limit=1000):
        return [
            doc.to_dict()
            for doc in self.client.collection(path)
            .limit(limit)
            .stream(transaction=self.transaction, retry=None, timeout=30)
        ]

    def set(self, path, value):
        self.writes.append(("set", path, copy.deepcopy(value)))

    def create(self, path, value):
        self.writes.append(("create", path, copy.deepcopy(value)))

    def where(self, path, field, value, limit=100):
        from google.cloud.firestore_v1.base_query import FieldFilter

        return [
            doc.to_dict()
            for doc in self.client.collection(path)
            .where(filter=FieldFilter(field, "==", value))
            .limit(limit)
            .stream(transaction=self.transaction, retry=None, timeout=30)
        ]

    def flush(self):
        for method, path, value in self.writes:
            if self.transaction is not None:
                getattr(self.transaction, method)(self.client.document(path), value)
            else:
                getattr(self.client.document(path), method)(value)


class FirestoreRepository(FirestoreUnit):
    def __init__(self, settings):
        from google.cloud import firestore

        super().__init__(firestore.Client(project=settings.project, database=settings.database))

    def transact(self, callback: Callable):
        from google.api_core.exceptions import Aborted
        from google.cloud import firestore

        @firestore.transactional
        def apply(transaction):
            unit = FirestoreUnit(self.client, transaction)
            result = callback(unit)
            unit.flush()  # Every read completed before the first SDK write.
            return result

        try:
            return apply(self.client.transaction(max_attempts=5))
        except (Aborted, ValueError) as exc:
            # The official decorator retries commit-time aborts. A query/read-time abort
            # or exhausted retries is a safe retryable API failure, never a partial effect.
            if isinstance(exc, Aborted) or isinstance(exc.__cause__, Aborted):
                raise AppError(
                    "TEMPORARY_DEPENDENCY_FAILURE",
                    "The transaction was contended; retry the same idempotent request.",
                    503,
                ) from exc
            raise

    def accessible_projects(self, user_id):
        from google.cloud.firestore_v1.base_query import FieldFilter

        snapshots = (
            self.client.collection_group("memberships")
            .where(filter=FieldFilter("user_id", "==", user_id))
            .limit(100)
            .stream()
        )
        return [doc.reference.parent.parent.id for doc in snapshots]

    def recent(self, path, field, limit):
        from google.cloud import firestore

        return [
            doc.to_dict()
            for doc in self.client.collection(path)
            .order_by(field, direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        ]


class FirebaseIdentity:
    def __init__(self, project):
        import firebase_admin

        self.app = firebase_admin.initialize_app(
            options={"projectId": project, "httpTimeout": 30}, name="handoff-guardian"
        )

    def verify(self, token, privileged):
        from firebase_admin import auth

        try:
            claims = auth.verify_id_token(token, app=self.app, check_revoked=privileged)
        except Exception as exc:
            raise AppError("UNAUTHENTICATED", "A valid Identity Platform sign-in is required.", 401) from exc
        firebase_claims = claims.get("firebase")
        require(
            isinstance(firebase_claims, dict) and firebase_claims.get("sign_in_provider") == "password",
            "UNAUTHENTICATED",
            "Sign in using your email address and password.",
            401,
        )
        require(
            claims.get("email_verified") is True,
            "EMAIL_NOT_VERIFIED",
            "Verify your email address before accessing project evidence.",
            403,
        )
        return claims["uid"]


class GCSStore:
    def __init__(self, settings):
        from google.cloud import storage

        self.bucket = storage.Client(project=settings.project).bucket(settings.bucket)

    def put(self, name, content, mime_type):
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(name)
        try:
            blob.upload_from_string(
                content, content_type=mime_type, if_generation_match=0, retry=None, timeout=60
            )
        except PreconditionFailed:
            blob.reload(retry=None, timeout=30)
            existing = blob.download_as_bytes(if_generation_match=blob.generation, retry=None, timeout=60)
            require(digest(existing) == digest(content), "VERSION_CONFLICT", "Immutable object conflict.")
        return {"name": name, "generation": int(blob.generation), "sha256": digest(content)}

    def read(self, ref):
        blob = self.bucket.blob(ref["name"], generation=ref["generation"])
        value = blob.download_as_bytes(if_generation_match=ref["generation"], retry=None, timeout=60)
        require(digest(value) == ref["sha256"], "INVALID_EVIDENCE", "Immutable object hash mismatch.", 422)
        return value


class TasksDispatcher:
    def __init__(self, settings):
        from google.cloud import tasks_v2

        self.client, self.settings = tasks_v2.CloudTasksClient(), settings

    def enqueue(self, job_id, generation):
        from google.api_core.exceptions import AlreadyExists
        from google.cloud import tasks_v2
        from google.protobuf.duration_pb2 import Duration

        s = self.settings
        parent = self.client.queue_path(s.project, s.queue_location, s.queue)
        task = {
            "name": f"{parent}/tasks/{job_id}-{generation}",
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{s.worker_url}/internal/jobs/{job_id}/run",
                "headers": {"Content-Type": "application/json"},
                "body": canonical({"dispatch_generation": generation}),
                "oidc_token": {"service_account_email": s.task_invoker, "audience": s.worker_url},
            },
            "dispatch_deadline": Duration(seconds=300),
        }
        try:
            self.client.create_task(parent=parent, task=task, retry=None, timeout=30)
        except AlreadyExists:
            pass


class DocumentAIExtractor:
    def __init__(self, settings):
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai

        self.settings = settings
        self.client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=f"{settings.docai_location}-documentai.googleapis.com")
        )

    def extract(self, raw):
        from google.cloud import documentai
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(raw), strict=True)
            require(not reader.is_encrypted, "ENCRYPTED_PDF", "Encrypted PDFs are unsupported.", 422)
            count = len(reader.pages)
            require(
                0 < count <= self.settings.max_source_pages, "PDF_PAGE_LIMIT", "PDF page limit exceeded.", 422
            )
        except AppError:
            raise
        except Exception as exc:
            raise AppError("EXTRACTION_FAILED", "The uploaded PDF cannot be parsed safely.", 422) from exc
        response = self.client.process_document(
            request=documentai.ProcessRequest(
                name=self.settings.processor_version,
                raw_document=documentai.RawDocument(content=raw, mime_type="application/pdf"),
            ),
            retry=None,
            timeout=self.settings.document_ai_timeout,
        )
        document = response.document
        require(
            len(document.pages) == count and not document.error.code,
            "EXTRACTION_FAILED",
            "Document AI did not return complete pages.",
            422,
        )
        text, spans = document.text, []
        require(
            0 < len(text) <= 200000 and bool(text.strip()),
            "EMPTY_EXTRACTION",
            "Document AI returned empty or oversized text.",
            422,
        )
        for number, page in enumerate(document.pages, 1):
            # The Python SDK exposes text-anchor character indices into Document.text.
            # Preserve that exact string: no newline or Unicode normalization is applied.
            segments = page.layout.text_anchor.text_segments
            start = min(
                (int(segment.start_index) for segment in segments),
                default=spans[-1]["end_char"] if spans else 0,
            )
            end = max((int(segment.end_index) for segment in segments), default=start)
            require(
                0 <= start <= end <= len(text), "EXTRACTION_FAILED", "Invalid Document AI page anchors.", 422
            )
            spans.append({"page_number": number, "start_char": start, "end_char": end})
        return text, spans, documentai.Document.to_json(document).encode()


class AgentEngineProvider:
    def __init__(self, settings):
        import agentplatform

        self.settings = settings
        self.client = agentplatform.Client(
            project=settings.project,
            location=settings.location,
            http_options={"timeout": settings.model_timeout * 1000, "retry_options": {"attempts": 1}},
        )
        self.agent = self.client.runtimes.get(name=settings.agent_resource)

    async def analyze(self, packet, attempt_id):
        async with asyncio.timeout(self.settings.model_timeout):
            session = await self.agent.async_create_session(user_id=attempt_id)
            final = None
            provenance = {
                "usage": "unknown",
                "model": self.settings.model,
                "agent_resource": self.settings.agent_resource,
            }
            async for event in self.agent.async_stream_query(
                user_id=attempt_id, session_id=session["id"], message=canonical(packet).decode()
            ):
                require(
                    not event.get("error_code"), "PROVIDER_UNAVAILABLE", "The deployed agent failed.", 503
                )
                if event.get("partial") or event.get("author") != "handoff_guardian":
                    continue
                metadata = event.get("custom_metadata", {})
                for key in ("model_calls", "usage", "packet_fingerprint", "prompt_version", "model"):
                    if key in metadata:
                        provenance[key] = metadata[key]
                content = event.get("content", {})
                parts = content.get("parts", [])
                text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
                if text and not any("function_call" in part or "function_response" in part for part in parts):
                    require(
                        len(text.encode()) <= 512 * 1024,
                        "PROVIDER_OUTPUT_INVALID",
                        "Oversized analysis.",
                        422,
                    )
                    try:
                        candidate = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(candidate, dict) and set(candidate) == {
                        "proposed_commitments",
                        "findings",
                        "coverage_warnings",
                    }:
                        final = candidate
            require(
                final is not None,
                "PROVIDER_OUTPUT_INVALID",
                "Agent returned no final structured analysis.",
                422,
            )
            return ProviderResponse(final, provenance)
