# Handoff Guardian — AI agent

**Technical review guide for the AI Builder Cup submission.** Start with the [project overview and judge walkthrough](../README.md). This component contains the project's one deployed, read-only AI agent.

## What the AI contributes

Handoff documents often describe the same deliverable in different words. The agent compares promises, reviewed scope, acceptance requirements and dependencies, then returns proposed commitments and evidence-grounded findings:

| Finding | Meaning |
| --- | --- |
| Scope mismatch | Sources describe incompatible deliverables, dates or conditions. |
| Acceptance gap | The supplied evidence does not establish a relevant acceptance requirement. |
| Dependency conflict | A prerequisite conflicts with the stated plan or milestone. |

There is **one ADK workflow**, not a collection of specialist agents. Understanding evidence, detecting discrepancies and grounding findings are capabilities within that workflow. The Cloud Run worker coordinates jobs; it does not run a second agent.

## Input → reasoning → checked output

1. The worker supplies a freshly authorized, immutable project snapshot through a fresh managed session.
2. Agent code whitelists the packet, checks source ownership and canonical hashes, and excludes operational records and test labels.
3. Gemini compares the supplied evidence and returns structured proposed commitments, findings and coverage warnings, using source IDs and exact quotes.
4. Deterministic code resolves each unique quote to Unicode code-point offsets. Invalid references, ambiguous quotes, invalid owners/dates and schema violations are rejected.
5. Within the same bounded invocation, a controlled repair instruction may request a corrected result. Only validated final JSON and safe provenance are emitted.
6. The worker independently validates the result again before publication and rechecks permissions, evidence freshness and its job lease.

The transport schema guides generation; strict post-generation validation enforces the full result contract. A schema-shaped answer is not accepted as proof by itself.

## Authority and safety boundaries

- Source text is untrusted data. It cannot grant tools, access, authority or approval.
- The agent has no source-retrieval, Firestore-write, task-write or approval tools.
- A proposed commitment is not accepted scope. Only a reviewer makes authority and lifecycle decisions.
- Expected fixture labels, canned responses and foreign-project decoys never enter the deployed model packet.
- At most **three model requests within 60 seconds** are permitted per invocation, including repair. SDK request retries are limited to one attempt.
- A logical worker job can reserve up to three external attempts, so ambiguous failures can incur repeated inference. No exactly-once inference or zero-duplicate-cost guarantee is claimed.
- Application tracing and deployed automatic content telemetry are disabled. Safe diagnostics contain controlled validation codes, bounded field/type locations and numeric usage, not source bodies or rejected model text.

## Implementation to inspect

| File | Review focus |
| --- | --- |
| [workflow.py](src/handoff_agent/workflow.py) | Custom ADK agent, bounded Gemini calls, managed runtime construction and final event |
| [prompt.py](src/handoff_agent/prompt.py) | Untrusted-evidence handling, finding semantics and reviewer authority |
| [schemas.py](src/handoff_agent/schemas.py) | Packet whitelist, strict output schema, unique exact-quote resolution and reference validation |
| [diagnostics.py](src/handoff_agent/diagnostics.py) | Safe rejection taxonomy and targeted repair instructions |
| [tests/](tests/) | Grounding, workflow, transport-schema and diagnostic checks |
| [uv.lock](uv.lock) / [requirements.txt](requirements.txt) | Locked development dependencies and production dependency export |

The recorded deployment uses Gemini 3.8 Flash on Vertex AI in `us`, with ADK on Vertex AI Agent Engine in `us-central1`. Project, model and location are configured explicitly. Attached identity is used; API keys, downloaded credential files and emulator endpoints are rejected by managed startup.

## Local checks

From the repository root, with Python 3.12 and uv:

~~~sh
uv --directory agent sync --frozen
uv --directory agent run ruff check .
uv --directory agent run pytest
uv --directory agent run python ../scripts/deploy_agent.py --help
~~~

The tests use isolated inputs and test doubles; they do not request live Gemini inference. The deployment helper's help command lists its inputs without deploying resources or making model calls.