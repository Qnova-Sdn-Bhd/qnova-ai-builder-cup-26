# Handoff Guardian — Synthetic demo and test fixtures

**Supporting review material for the AI Builder Cup submission.** Start with the [project overview and judge walkthrough](../README.md). This folder provides fictional evidence for demonstrations and repeatable offline checks.

Every organisation, person, email address, project, date and business record here is synthetic. The `.test` addresses are fictional attribution, **not login accounts**. The offline inventory contains 12 scenarios, 30 Markdown source versions, 12 expected-result files and five canned responses. Its 12 projects comprise 11 subjects and one foreign-project decoy; scenarios 01 and 12 share Harbour material. The judge-ready packet in `demo-upload/` contains prepared copies for the live demonstration, not additional benchmark scenarios.

These are developer-visible examples, not a hidden test set or an independent real-world accuracy benchmark. Canned responses never replace a failed live model call.

## Judge demo: use the ready-to-upload files

The initial Harbour packet illustrates a launch promise that differs from documented scope, incomplete acceptance proof and conditional dependency risks.

| File to upload | Document type | What judges can inspect |
| --- | --- | --- |
| [01-harbour-scope.pdf](demo-upload/01-harbour-scope.pdf) | `sow` | Password login at launch; Entra ID SSO scheduled for phase two; exercises real Document AI processing |
| [02-launch-email.md](demo-upload/02-launch-email.md) | `email` | SSO promised for launch without an incorporated change order |
| [03-readiness-checklist.md](demo-upload/03-readiness-checklist.md) | `acceptance_checklist` | “Complete” status supported by password-login testing rather than SSO acceptance |
| [04-dependency-plan.md](demo-upload/04-dependency-plan.md) | `project_export` | Credentials and security approval planned after the stated launch |

Download these four files from [demo-upload/](demo-upload/). The [upload-details.csv](demo-upload/upload-details.csv) is a reference for document title, ID, type and UTC dates when completing the upload form. **Do not upload the CSV as evidence.** Use version 1 for these initial documents and no earlier source version. The folder contains the prepared PDF; judges do not need to generate it or install development tools.

To try the real application:

1. Open the [live application](https://hg-web-622303555908.us-central1.run.app) and **sign in with the pre-registered judge account provided in the submission presentation deck**. Judges do not need to register an account. The submitter prepares email verification and reviewer membership before judging; no actual credentials are stored in this README.
2. Select the assigned empty demo project. In **Sources**, choose **Upload source** and upload the four files above, using the CSV as a metadata reference. Upload each once; do not add the original Markdown scope baseline alongside the PDF. If no assigned project is visible, contact the submitter to check the prepared account's access.
3. Wait for ingestion to finish, inspect the original/canonical text and explicitly review source authority. A title such as “Signed scope” does not make an upload an accepted baseline.
4. Select **Run analysis** and review actual findings, citations and coverage warnings. Generated IDs, finding counts and wording need not match a canned response.
5. Review a blocker-task proposal, approve its exact payload and separately execute it. Inspect the persisted task and audit history. A task does not close a finding or commitment.

Acceptance and dependency issues involving an unaccepted launch promise must remain conditional; they are not automatically a breach of accepted phase-two scope. Later amendments and UAT evidence belong to later uploads and reviews, not the initial packet.

## Folder guide and evidence boundaries

| Location | Purpose | Treatment in the live prototype |
| --- | --- | --- |
| [demo-upload/](demo-upload/) | Judge-ready packet: one PDF, three Markdown files and a metadata reference CSV | Upload only the four evidence files through Sources; the CSV is a form-entry reference. |
| [inputs/manifest.json](inputs/manifest.json) | Offline inventory, scenarios, fictional memberships and source metadata | Never send the whole inventory to the model or treat its identities/roles as authentication. |
| [inputs/sources/](inputs/sources/) | Synthetic source documents | Upload only the authorized project's current-stage files through normal ingestion. |
| [inputs/events/](inputs/events/) | Declarative lifecycle and review sequences | Offline test-control data; not provider evidence, executable instructions or credentials. |
| [expected/](expected/) | Reference outcomes, exact Markdown spans and lifecycle expectations | Evaluator-only; excluded from deployed model context and runtime evidence. |
| [mock/](mock/) | Canned structured responses for exact offline snapshots | Explicit test doubles; never a production fallback or live accuracy/latency/cost evidence. |
| [security_boundary/](security_boundary/) | Kestrel evidence outside Juniper's authorized project | Negative-test material; must not enter Juniper's packet or satisfy its acceptance requirements. |

Only `scope_mismatch`, `acceptance_gap` and `dependency_conflict` are application finding kinds. Labels such as `needs_review` are evaluation annotations, not additional finding types.

## Scenario coverage

| Scenario | Subject | Behavior being examined |
| --- | --- | --- |
| `scn_01` | Harbour | Presales SSO launch promise versus accepted phase-two scope |
| `scn_02` | Bay | Password-login completion does not establish SSO acceptance |
| `scn_03` | Cedar | Prerequisites scheduled after go-live |
| `scn_04` | Delta | Supported Azure AD / Entra ID / corporate SSO equivalence without a false mismatch |
| `scn_05` | Elm | Unsigned proposal cannot replace accepted scope |
| `scn_06` | Fir | Reviewed replacement applies only when effective |
| `scn_07` | Grove | Missing evidence is not proof of implementation failure |
| `scn_08` | Heath | Ambiguous authority requires clarification |
| `scn_09` | Iris | Document-embedded prompt injection remains untrusted content |
| `scn_10` | Juniper / Kestrel | Foreign-project evidence cannot close an authorized project's gap |
| `scn_11` | Larch | Duplicate requests, permissions, separate approval/execution, replay and stale bindings |
| `scn_12` | Harbour | Staged source review, task creation, supersession and evidence-based closure |

## Document AI processing and developer PDF preparation

The supplied [scope PDF](demo-upload/01-harbour-scope.pdf) is ready for judge upload. Its processing uses Cloud Storage, Cloud Tasks, the private worker and the pinned Document AI processor version. The other three files use direct UTF-8 parsing. The prototype limits are 10 MiB and 15 PDF pages. Every PDF uses Document AI, including PDFs with selectable text.

For developers preparing another copy, the [PDF helper](../scripts/create_demo_pdf.mjs) runs with `node scripts/create_demo_pdf.mjs` from the repository root after frontend dependencies and Playwright Chromium are installed. Its default output is `artifacts/synthetic-harbour-sow.pdf`. This optional preparation is not part of the judge walkthrough. Generation does not upload the file, call Document AI or establish a passing cloud test.

Inspect the actual extraction and page mapping. **Do not reuse Markdown offsets, hashes or line numbers for the PDF.** Citation spans must be anchored to its processor-derived canonical text. Runtime IDs and revisions come from the application, not fixture aliases.

Pending or failed active ingestion blocks analysis. A reviewer may retry a terminal failure through a new job or withdraw eligible failed/unreviewed evidence with a reason and a coverage warning. Ready or accepted evidence cannot be withdrawn through that failure-recovery path.

## Validate and prepare offline inputs

From the repository root, using Python 3.12:

~~~sh
python scripts/validate_fixtures.py --json
python scripts/prepare_evaluation.py --case scn_01 --output-dir artifacts/evaluation
~~~

The [fixture validator](../scripts/validate_fixtures.py) checks inventory consistency, synthetic markers, IDs, source hashes, Unicode spans, memberships, lineage, stage boundaries and canned snapshot fingerprints against the [fixture contract](../contracts/fixtures.schema.json). It does not call Google services or execute authentication, database transactions or model inference.

The [packet-preparation helper](../scripts/prepare_evaluation.py) selects only the authorized initial project/stage and verifies hashes. Generated packets are offline synthetic inputs, not deployed source records or PDF annotations. It makes no model calls and does not include expected labels or canned responses.

The separate [cloud-pack validator](../scripts/validate_cloud_pack.py) checks starter-pack artifacts, not deployed behavior. It still expects original specification/harness documents absent from this submission checkout. Its reproduction gap is explained in the [infrastructure guide](../infra/terraform/README.md#operator-deployment-handoff-and-current-gaps); it should not be confused with fixture integrity.

## Interpreting results and staged reviews

- **Exact evidence:** fixture spans use zero-based Unicode code-point `[start_char, end_char)` offsets and exact quotes. Later Harbour evidence includes a non-BMP character to catch UTF-16 indexing errors.
- **Human authority:** offline metadata describes fictional reviewed facts. Normal uploads begin unreviewed; actual authorized reviewers decide authority, accepted commitments and closure.
- **Lineage:** Harbour and Fir amendments are consolidated replacements in the same logical document stream. A newer upload does not supersede accepted evidence automatically.
- **Business time:** scenario dates are synthetic. Only trusted operator tooling sets or advances the synthetic project clock; changes increment the evidence revision and invalidate old approval bindings. Authentication, leases and audit timestamps use real UTC.
- **Events and aliases:** translate offline sequences through the [current API contract](../contracts/openapi.yaml), actual runtime IDs and server-returned bindings. Do not execute capture expressions or treat event wrappers as deployed endpoints.
- **Canned snapshots:** offline adapters must reject unsupported snapshots rather than choose a nearby answer. The deployed application has no canned provider.
- **Benchmark claims:** the [scoring helper](../scripts/score_evaluation.py) requires reviewed adjudication and distinguishes canned from live Gemini provenance. Scenarios 01 and 12 overlap; their results are not independent samples. Preparing packets or passing fixture validation is not measured model accuracy or user impact.

For recorded deployment observations and remaining live acceptance gaps, see the [root verification status](../README.md#verification-status). The [cloud acceptance runner](../scripts/run_cloud_acceptance.py) requires real resources and authenticated reviewers for live probes; offline fixtures alone cannot establish passing results.
