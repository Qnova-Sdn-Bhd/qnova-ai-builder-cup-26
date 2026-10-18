import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_evidence import output

from handoff_agent.schemas import EvidenceError
from handoff_agent.workflow import HandoffAgent, analyze, create_app, validate_runtime


def client_with(*responses):
    generate = AsyncMock(
        side_effect=[
            SimpleNamespace(text=text, usage_metadata=None, model_version="gemini-unit-test")
            for text in responses
        ]
    )
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))


async def test_repairs_are_bounded_and_usage_unknown(packet):
    client = client_with("bad", "bad", json.dumps(output()))
    result, provenance = await analyze(packet, client, "gemini-unit-test")
    assert result["findings"][0]["evidence"][0]["start_char"] == 10
    assert provenance["model_calls"] == 3
    assert provenance["usage"] == [None, None, None]
    assert client.aio.models.generate_content.await_count == 3
    config = client.aio.models.generate_content.await_args_list[0].kwargs["config"]
    assert config.temperature is None
    assert config.thinking_config.thinking_level.value == "LOW"
    assert config.thinking_config.include_thoughts is False


async def test_invalid_provider_exhausts_budget(packet, capsys):
    client = client_with("bad", "bad", "bad")
    with pytest.raises(EvidenceError, match="PROVIDER_OUTPUT_INVALID"):
        await analyze(packet, client, "gemini-unit-test")
    assert client.aio.models.generate_content.await_count == 3
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["attempt"] for event in events] == [1, 2, 3]
    assert all(event["reason"] == "OUTPUT_SCHEMA_INVALID" for event in events)


async def test_timeout_does_not_fallback(packet):
    client = client_with()
    client.aio.models.generate_content.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await analyze(packet, client, "gemini-unit-test")
    assert client.aio.models.generate_content.await_count == 1


async def test_actual_sdk_serializes_lean_generation_schema_without_network(packet):
    import httpx
    from google import genai
    from google.oauth2.credentials import Credentials

    fake = client_with(json.dumps(output()))
    await analyze(packet, fake, "gemini-unit-test")
    config = fake.aio.models.generate_content.await_args.kwargs["config"]
    requests = []

    def capture(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": json.dumps(output())}]}}
                ]
            },
        )

    # A fake credential and an in-memory HTTP transport prevent any ADC or cloud use.
    with genai.Client(
        vertexai=True,
        project="offline-test-project",
        location="us",
        credentials=Credentials(token="offline-only-not-real-token"),
        http_options={
            "api_version": "v1",
            "httpx_client": httpx.Client(transport=httpx.MockTransport(capture)),
        },
    ) as sdk:
        sdk.models.generate_content(
            model="gemini-3.8-flash", contents="Offline transport test", config=config
        )

    assert len(requests) == 1
    assert requests[0].url.host == "aiplatform.us.rep.googleapis.com"
    body = json.loads(requests[0].content)
    generated = body["generationConfig"]
    assert generated["responseMimeType"] == "application/json"
    schema = generated["responseSchema"]
    supported = {"type", "properties", "required", "items", "enum", "nullable", "property_ordering"}

    def check_shape(node):
        assert set(node) <= supported
        for child in node.get("properties", {}).values():
            check_shape(child)
        if "items" in node:
            check_shape(node["items"])

    check_shape(schema)
    assert schema["required"] == ["proposed_commitments", "findings", "coverage_warnings"]
    commitment = schema["properties"]["proposed_commitments"]["items"]
    assert commitment["properties"]["owner_id"] == {"type": "STRING", "nullable": True}
    assert commitment["properties"]["due_date"] == {"type": "STRING", "nullable": True}
    finding = schema["properties"]["findings"]["items"]
    assert finding["properties"]["kind"]["enum"] == [
        "scope_mismatch",
        "acceptance_gap",
        "dependency_conflict",
    ]
    assert "title" in finding["properties"]
    evidence = finding["properties"]["evidence"]["items"]
    assert evidence["required"] == ["source_version_id", "quote"]


def test_managed_adk_wrapper_and_no_write_tools(monkeypatch):
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    app = create_app("p", "us-central1", "gemini-unit-test")
    agent = app._tmpl_attrs["agent"]
    assert agent.name == "handoff_guardian"
    assert agent.location == "us-central1" and agent.model_location == "us"
    assert not hasattr(agent, "tools")


def test_runtime_rejects_keys_and_emulators(monkeypatch):
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:8080")
    with pytest.raises(ValueError):
        validate_runtime("p", "us-central1", "gemini-unit-test")


async def test_adk_emits_one_final_event_with_safe_provenance(packet, monkeypatch):
    from google.genai import types

    client = client_with(json.dumps(output()))
    client.aio.aclose = AsyncMock()
    constructor_calls = []

    def build_client(**kwargs):
        constructor_calls.append(kwargs)
        return client

    monkeypatch.setattr("handoff_agent.workflow.genai.Client", build_client)
    agent = HandoffAgent(
        name="handoff_guardian", project="p", location="us-central1", model_name="gemini-unit-test"
    )
    ctx = SimpleNamespace(
        invocation_id="inv-1",
        user_content=types.Content(role="user", parts=[types.Part(text=json.dumps(packet))]),
    )
    events = [event async for event in agent._run_async_impl(ctx)]
    assert len(events) == 1
    assert events[0].is_final_response()
    assert events[0].author == "handoff_guardian"
    assert events[0].custom_metadata["model_calls"] == 1
    configured = constructor_calls[0]
    assert configured["vertexai"] is True
    assert configured["project"] == "p" and configured["location"] == "us"
    assert configured["http_options"].api_version == "v1"
    assert configured["http_options"].timeout == 55000
    assert configured["http_options"].retry_options.attempts == 1
    client.aio.aclose.assert_awaited_once()


def test_genai_sdk_uses_documented_us_multi_region_endpoint():
    from google import genai
    from google.auth.credentials import AnonymousCredentials

    client = genai.Client(
        vertexai=True,
        project="offline-test-project",
        location="us",
        credentials=AnonymousCredentials(),
    )
    assert client._api_client._http_options.base_url == "https://aiplatform.us.rep.googleapis.com/"
    client.close()


def test_deployment_manifest_separates_runtime_and_inference_locations(monkeypatch):
    import importlib.util
    from pathlib import Path

    file = Path(__file__).resolve().parents[2] / "scripts" / "deploy_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_agent_test_module", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = SimpleNamespace(
        project="test-project",
        location="us-central1",
        model="gemini-3.8-flash",
        model_location="us",
        service_account="agent@test-project.iam.gserviceaccount.com",
        staging_bucket="private-staging",
        resource_name=None,
    )
    config = module.manifest(args)
    assert isinstance(config["requirements"], list)
    assert "google-adk==2.12.0" in config["requirements"]
    assert all(line and not line.startswith("#") for line in config["requirements"])
    assert not {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "PORT",
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    }.intersection(config["env_vars"])
    assert config["env_vars"]["GEMINI_LOCATION"] == "us"
    telemetry_flag = "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"
    assert config["env_vars"][telemetry_flag] == "false"
    monkeypatch.setenv(telemetry_flag, config["env_vars"][telemetry_flag])
    app = create_app(args.project, args.location, args.model, model_location=args.model_location)
    assert app._tmpl_attrs["enable_tracing"] is False
    assert app._telemetry_enabled() is False
    assert app._tracing_enabled() is False
    args.model_location = "us-central1"
    with pytest.raises(ValueError, match="model-location"):
        module.manifest(args)


def test_deployment_requirement_filter_preserves_pins_markers_and_sdk_list_support(
    tmp_path, monkeypatch, caplog
):
    import importlib.util
    from pathlib import Path

    from agentplatform._genai import _runtimes_utils

    script = Path(__file__).resolve().parents[2] / "scripts" / "deploy_agent.py"
    spec = importlib.util.spec_from_file_location("deploy_requirements_test_module", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    export = tmp_path / "requirements.txt"
    marker = "cffi==2.1.1 ; platform_python_implementation != 'PyPy'"
    export.write_text(
        "# Generated by uv\n\n  google-adk==2.12.0\n    # via managed agent\n"
        + marker
        + "\n    # via cryptography\n",
        encoding="utf-8",
    )
    filtered = module.deployment_requirements(export)
    assert filtered == ["google-adk==2.12.0", marker]
    monkeypatch.setattr(_runtimes_utils, "_scan_requirements", lambda **kwargs: {})
    assert (
        _runtimes_utils._validate_requirements_or_raise(agent=object(), requirements=filtered)
        == filtered
    )
    assert "Failed to parse constraint" not in caplog.text
    export.write_text("google-adk>=2.12\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact pinned"):
        module.deployment_requirements(export)


def test_current_model_rejects_unsupported_regional_inference_location(monkeypatch):
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="model location"):
        create_app("test-project", "us-central1", "gemini-3.8-flash", model_location="us-central1")
