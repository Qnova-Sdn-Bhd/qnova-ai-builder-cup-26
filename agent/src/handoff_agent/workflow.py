"""One bounded ADK workflow. All Gemini calls execute inside managed Agent Engine."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator

from google import genai
from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types
from pydantic import ValidationError

from handoff_agent import PROMPT_VERSION
from handoff_agent.diagnostics import log_rejection, repair_instruction, validation_failure
from handoff_agent.prompt import SYSTEM_INSTRUCTION
from handoff_agent.schemas import (
    EvidenceError,
    canonical_json,
    generation_schema,
    resolve_result,
    validate_packet,
)

logger = logging.getLogger(__name__)


def validate_runtime(project: str, location: str, model: str, model_location: str = "us") -> None:
    if (
        not project
        or not location
        or not model_location
        or not model
        or "gemini" not in model.lower()
    ):
        raise ValueError("Google project, location and available Gemini model are required")
    if model == "gemini-3.8-flash" and model_location not in {"us", "eu", "global"}:
        raise ValueError(
            "Gemini 3.8 Flash requires a supported multi-region or global model location"
        )
    forbidden = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
    if any(os.environ.get(key) for key in forbidden):
        raise ValueError("Managed agent requires attached credentials without keys or key files")
    if any("EMULATOR" in key and value for key, value in os.environ.items()):
        raise ValueError("Managed agent forbids emulator endpoints")


async def analyze(packet: dict, client, model: str) -> tuple[dict, dict]:
    safe = validate_packet(packet)
    fingerprint = hashlib.sha256(canonical_json(safe).encode("utf-8")).hexdigest()
    request_text = canonical_json(safe)
    usage, calls = [], 0
    async with asyncio.timeout(60):
        for attempt in range(3):
            calls += 1
            response = await client.aio.models.generate_content(
                model=model,
                contents=request_text,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=generation_schema(),
                    max_output_tokens=16384,
                    thinking_config=types.ThinkingConfig(
                        include_thoughts=False, thinking_level=types.ThinkingLevel.LOW
                    ),
                ),
            )
            usage.append(
                response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None
            )
            try:
                result = resolve_result(response.text or "", safe)
                return result, {
                    "prompt_version": PROMPT_VERSION,
                    "model": getattr(response, "model_version", None) or model,
                    "model_calls": calls,
                    "usage": usage,
                    "packet_fingerprint": fingerprint,
                }
            except (EvidenceError, ValidationError, ValueError) as exc:
                failure = validation_failure(exc)
                log_rejection(attempt + 1, failure, usage[-1])
                if attempt == 2:
                    raise EvidenceError("PROVIDER_OUTPUT_INVALID") from None
                # Only controlled diagnostics are echoed; never rejected output or values.
                request_text = canonical_json(safe) + repair_instruction(failure)
    raise EvidenceError("PROVIDER_OUTPUT_INVALID")


class HandoffAgent(BaseAgent):
    project: str
    location: str
    model_location: str = "us"
    model_name: str

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        validate_runtime(self.project, self.location, self.model_name, self.model_location)
        content = ctx.user_content
        if content is None or not content.parts:
            raise EvidenceError("MISSING_PACKET")
        raw = "".join(part.text or "" for part in content.parts)
        if len(raw.encode("utf-8")) > 2_000_000:
            raise EvidenceError("PACKET_TOO_LARGE")
        packet = json.loads(raw)
        client = genai.Client(
            vertexai=True,
            project=self.project,
            location=self.model_location,
            http_options=types.HttpOptions(
                api_version="v1", timeout=55000, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        try:
            result, provenance = await analyze(packet, client, self.model_name)
        finally:
            await client.aio.aclose()
        logger.info(
            "analysis_completed",
            extra={
                "prompt_version": PROMPT_VERSION,
                "model_calls": provenance["model_calls"],
                "packet_fingerprint": provenance["packet_fingerprint"],
            },
        )
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=canonical_json(result))]),
            partial=False,
            turn_complete=True,
            custom_metadata=provenance,
        )


def create_app(project: str, location: str, model: str, model_location: str = "us"):
    from agentplatform.frameworks.adk import AdkApp

    validate_runtime(project, location, model, model_location)
    return AdkApp(
        agent=HandoffAgent(
            name="handoff_guardian",
            project=project,
            location=location,
            model_location=model_location,
            model_name=model,
        ),
        # Whole source packets must not enter automatic tracing/logging.
        enable_tracing=False,
    )
