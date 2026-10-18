# Handoff Guardian — API and private worker

**Technical review guide for the AI Builder Cup submission.** Start with the [project overview and judge walkthrough](../README.md). This component implements the authorization, durable automation and reviewed task-writing boundaries around the AI.

## Two services, one codebase

| Service | Responsibility |
| --- | --- |
| Public API: `hg-api` | Verify Identity Platform identity, load project membership, serve authorized evidence, record reviews and execute approved actions. |
| Private worker: `hg-worker` | Process queued ingestion/analysis jobs, invoke Document AI or the managed agent, validate results and publish under a durable lease. |

Both deploy to Cloud Run from the same backend image with different entry points and attached service accounts. The worker requires Cloud Run IAM authorization plus the configured Google OIDC caller/audience checks. Browser tokens and role headers cannot authorize worker execution.

All Gemini inference runs in the ADK agent on Vertex AI Agent Engine. The worker is a deterministic coordinator, not a model or agent runtime.

## Evidence and analysis flow

1. The API verifies the ID token, current UID binding and project membership before reading evidence or accepting a write. Email must be verified and the sign-in provider must be password-based.
2. Originals are written to private Cloud Storage with creation preconditions, generation references and hashes. A Firestore transaction creates source/job/outbox records and advances the evidence revision.
3. Cloud Tasks invokes the worker. Every PDF uses the pinned Document AI processor version; TXT/Markdown use bounded UTF-8 parsing. Canonical text and page anchors retain exact citation positions.
4. A reviewer requests analysis. The API captures an immutable authorized snapshot; the worker rechecks access before reading it and invokes the configured managed agent in a fresh session.
5. The worker independently checks final output schema, references, hashes, quotes, Unicode spans and owners. It rechecks current permission, evidence revision and lease fencing before publishing.

An active pending or failed source blocks analysis. A reviewer can retry terminal failed ingestion through a new job or withdraw eligible failed evidence with a reason; withdrawal remains visible in coverage warnings. Neither upload nor model output assigns source authority.

## Human approval and durable task execution

The only action type is `create_blocker_task` on the application's Firestore board.

- A proposal binds its exact payload/hash to analysis, evidence revision, board revision and finding version.
- Approval records a reviewer decision. It creates no task.
- Separate execution rechecks current permissions, approval binding, owner membership and freshness.
- One Firestore transaction writes the task, action/execution result, idempotency receipt, board revision and audit event.
- Repeating the same completed execution returns its persisted result. Changed payloads or stale unexecuted approvals cannot produce a new task.
- Task creation does not resolve a finding or close a commitment. Closure requires separate review of acceptance evidence.

The Firestore outbox covers failed queue dispatch. Workers reserve attempts, claim leases and publish only through the current fence. Scheduler recovery handles unfinished work. Duplicate delivery and ambiguous timeouts can repeat paid external calls; durable publication does not imply exactly-once inference.

## Implementation to inspect

| File | Review focus |
| --- | --- |
| [api.py](src/handoff_guardian/api.py) / [worker.py](src/handoff_guardian/worker.py) | Separate public and private HTTP entry points |
| [service.py](src/handoff_guardian/service.py) | Membership authorization, source review, proposal binding and transactional execution |
| [jobs.py](src/handoff_guardian/jobs.py) | Durable attempts, outbox recovery, ingestion, analysis and fenced publication |
| [google_adapters.py](src/handoff_guardian/google_adapters.py) | Identity Platform, Firestore, Storage, Document AI, Tasks and Agent Engine integrations |
| [evidence.py](src/handoff_guardian/evidence.py) | Citation, baseline, date and independent provider-result checks |
| [config.py](src/handoff_guardian/config.py) / [observability.py](src/handoff_guardian/observability.py) | Protected startup settings and safe structured logs |
| [API contract](../contracts/openapi.yaml) / [tests/](tests/) | Interfaces and domain/SDK/emulator regression checks |

Firestore owns project entities, reviews, revisions, approvals, tasks and audit records; durable jobs/outbox and identity bindings are also held in Firestore. Cloud Storage owns original documents, canonical text, OCR output and archived analysis payloads. No local persistent database or fixture-backed runtime is provided.

## Local checks and configuration

From the repository root, with Python 3.12 and uv:

~~~sh
uv --directory backend sync --frozen
uv --directory backend run ruff check .
uv --directory backend run pytest
~~~

The native transaction suite **skips** unless Google's Firestore emulator is running. With an emulator listening on port 8089, use this PowerShell test-only configuration:

~~~powershell
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8089'
uv --directory backend run pytest
Remove-Item Env:FIRESTORE_EMULATOR_HOST
~~~

A skipped emulator case is not a pass. Never put emulator settings into deployed services.

For authorized local API development, populate the process environment using [the configuration template](../.env.example) and approved ADC, then run:

~~~sh
uv --directory backend run uvicorn handoff_guardian.api:app --host 127.0.0.1 --port 8080
~~~

Environment files are not loaded implicitly. Startup rejects missing Google configuration, test backends and emulator/credential-file overrides. It does not create accounts, resources or a local demo identity. `/health` checks process availability; `/ready` checks configuration and does not prove live dependency health.

Passwords, verification and reset are handled directly by Identity Platform's client SDK. The API receives bearer tokens and resolves roles server-side. Safe operational logs contain IDs, revisions, status, latency and failure codes; they exclude credentials, complete source bodies and hidden model reasoning.