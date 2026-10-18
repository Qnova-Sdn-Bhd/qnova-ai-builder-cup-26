import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from google.cloud import documentai
from pypdf import PdfWriter

from handoff_guardian.errors import AppError
from handoff_guardian.google_adapters import AgentEngineProvider, DocumentAIExtractor, FirestoreUnit, GCSStore


def pdf(pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    if encrypted:
        writer.encrypt("private-test-password")
    value = io.BytesIO()
    writer.write(value)
    return value.getvalue()


class OCR:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def process_document(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(document=self.document)


def test_docai_keeps_unicode_codepoint_text_and_page_mapping(settings):
    extractor = DocumentAIExtractor.__new__(DocumentAIExtractor)
    extractor.settings = settings
    text = "Reviewed café 🛠 acceptance\n"
    document = documentai.Document(
        text=text,
        pages=[
            documentai.Document.Page(
                page_number=1,
                layout=documentai.Document.Page.Layout(
                    text_anchor=documentai.Document.TextAnchor(
                        text_segments=[
                            documentai.Document.TextAnchor.TextSegment(start_index=0, end_index=len(text))
                        ]
                    )
                ),
            )
        ],
    )
    extractor.client = OCR(document)
    actual, spans, raw = extractor.extract(pdf())
    assert actual == text and spans == [{"page_number": 1, "start_char": 0, "end_char": len(text)}]
    assert "café" in json.loads(raw)["text"]
    call = extractor.client.calls[0]
    assert call["request"].name == settings.processor_version and call["retry"] is None


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"%PDF-invalid", "EXTRACTION_FAILED"),
        (pdf(encrypted=True), "ENCRYPTED_PDF"),
        (pdf(pages=16), "PDF_PAGE_LIMIT"),
    ],
)
def test_pdf_policy_precedes_document_ai_call(settings, raw, code):
    extractor = DocumentAIExtractor.__new__(DocumentAIExtractor)
    extractor.settings, extractor.client = settings, OCR(None)
    with pytest.raises(AppError, match=code):
        extractor.extract(raw)
    assert extractor.client.calls == []


def test_docai_partial_response_is_failure(settings):
    extractor = DocumentAIExtractor.__new__(DocumentAIExtractor)
    extractor.settings = settings
    extractor.client = OCR(documentai.Document(text="partial", pages=[]))
    with pytest.raises(AppError, match="EXTRACTION_FAILED"):
        extractor.extract(pdf())


class RemoteAgent:
    def __init__(self, events):
        self.events, self.sessions = events, []

    async def async_create_session(self, **kwargs):
        self.sessions.append(kwargs)
        return {"id": "fresh-managed-session"}

    async def async_stream_query(self, **kwargs):
        assert kwargs["session_id"] == "fresh-managed-session"
        for event in self.events:
            yield event


def test_agent_adapter_accepts_only_final_agent_author_and_captures_usage(settings):
    result = {"proposed_commitments": [], "findings": [], "coverage_warnings": []}
    provider = AgentEngineProvider.__new__(AgentEngineProvider)
    provider.settings = settings
    provider.agent = RemoteAgent(
        [
            {"author": "user", "content": {"parts": [{"text": "untrusted"}]}},
            {
                "author": "handoff_guardian",
                "content": {"parts": [{"text": json.dumps(result)}]},
                "custom_metadata": {
                    "model_calls": 1,
                    "usage": {"input_tokens": 150},
                    "prompt_version": "v1",
                    "packet_fingerprint": "a" * 64,
                },
            },
        ]
    )
    response = asyncio.run(provider.analyze({"project": {"id": "p"}}, "p:run:1"))
    assert response.result == result and response.provenance["model_calls"] == 1
    assert response.provenance["usage"] == {"input_tokens": 150}
    assert response.provenance["packet_fingerprint"] == "a" * 64
    assert provider.agent.sessions == [{"user_id": "p:run:1"}]


def test_agent_engine_sdk_has_one_http_attempt_and_explicit_deadline(settings, monkeypatch):
    import agentplatform

    calls = []

    def client(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(runtimes=SimpleNamespace(get=lambda name: RemoteAgent([])))

    monkeypatch.setattr(agentplatform, "Client", client)
    AgentEngineProvider(settings)
    assert calls[0]["http_options"] == {"timeout": 60000, "retry_options": {"attempts": 1}}


def test_agent_adapter_cannot_publish_user_json_or_error_event(settings):
    provider = AgentEngineProvider.__new__(AgentEngineProvider)
    provider.settings = settings
    provider.agent = RemoteAgent(
        [
            {
                "author": "user",
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {"proposed_commitments": [], "findings": [], "coverage_warnings": []}
                            )
                        }
                    ]
                },
            }
        ]
    )
    with pytest.raises(AppError, match="PROVIDER_OUTPUT_INVALID"):
        asyncio.run(provider.analyze({}, "attempt"))
    provider.agent = RemoteAgent([{"error_code": "MODEL_FAILED"}])
    with pytest.raises(AppError, match="PROVIDER_UNAVAILABLE"):
        asyncio.run(provider.analyze({}, "attempt"))


def test_firestore_sdk_writes_are_buffered_until_all_reads_finish():
    calls = []

    class Reference:
        def get(self, **kwargs):
            calls.append("read")
            return SimpleNamespace(exists=True, to_dict=lambda: {"version": 1})

    class Transaction:
        def set(self, ref, value):
            calls.append("write")

    unit = FirestoreUnit(SimpleNamespace(document=lambda path: Reference()), Transaction())
    unit.get("projects/p")
    unit.set("projects/p", {"version": 2})
    unit.get("projects/p/actions/a")
    assert calls == ["read", "read"]
    unit.flush()
    assert calls == ["read", "read", "write"]


def test_gcs_reads_generation_pinned_and_checks_hash():
    from handoff_guardian.contracts import digest

    calls = []

    class Blob:
        def download_as_bytes(self, **kwargs):
            calls.append(kwargs)
            return b"immutable"

    store = GCSStore.__new__(GCSStore)
    store.bucket = SimpleNamespace(blob=lambda name, generation: Blob())
    ref = {"name": "opaque/generated", "generation": 42, "sha256": digest(b"immutable")}
    assert store.read(ref) == b"immutable" and calls[0]["if_generation_match"] == 42
    ref["sha256"] = "0" * 64
    with pytest.raises(AppError, match="INVALID_EVIDENCE"):
        store.read(ref)
