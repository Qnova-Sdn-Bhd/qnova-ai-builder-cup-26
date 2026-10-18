# Google Cloud deployment runbook

These definitions have been checked locally with Terraform 1.14.3 and the signed
Google provider 8.7.0. The provider checksum lock is included. Syntax validation
does not prove an authorized plan, IAM availability, quotas, identity setup or a
deployment. Current provisioning outcomes and observed cloud failures belong in
`harness/build-log.md`; the checks described here do not establish deployment.

The project, billing/deployment authorization, approved region bundle, available
Gemini model, pinned enabled OCR version and real reviewer email account must
be resolved before provisioning. The candidate synthetic-data bundle is
`us-central1` with Document AI `us`; it is not a residency guarantee. An existing
CLI project/account is discovery evidence, not authorization to spend or deploy.

## State and identity bootstrap

Use an approved Google account and ADC or an attached/impersonated service
account. Never download service-account keys. On Windows use `gcloud.cmd` when
PowerShell execution policy prevents the `gcloud.ps1` wrapper. Do not alter the
execution policy to run these commands.

The following are operator command templates, not commands already run against
a target. Replace `PROJECT_ID`, `APPROVED_REGION`, `STATE_BUCKET` and account
values with the authorized inputs. Use a private GCS state bucket created outside
this stack so `terraform init` has an existing backend:

```sh
gcloud auth login
gcloud auth application-default login
gcloud projects describe PROJECT_ID --format='value(projectNumber)'
gcloud services enable serviceusage.googleapis.com storage.googleapis.com iam.googleapis.com aiplatform.googleapis.com --project=PROJECT_ID
gcloud storage buckets create gs://STATE_BUCKET --project=PROJECT_ID --location=APPROVED_REGION --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://STATE_BUCKET --versioning
gcloud storage buckets add-iam-policy-binding gs://STATE_BUCKET --member=user:AUTHORIZED_DEPLOYER_EMAIL --role=roles/storage.objectAdmin
gcloud beta services identity create --service=aiplatform.googleapis.com --project=PROJECT_ID
```

The last command materializes both the Vertex and Reasoning Engine service
agents before Terraform grants the latter staging-reader/custom-agent token
permissions. Its output may show only the Vertex service agent. Preserve the
automatically granted Google service-agent roles. Materialize Tasks/Scheduler
service identities too if the target's API-enable path does not create them:

```sh
gcloud beta services identity create --service=cloudtasks.googleapis.com --project=PROJECT_ID
gcloud beta services identity create --service=cloudscheduler.googleapis.com --project=PROJECT_ID
```

Only the deployment operator needs state-bucket access. The application and build
runtime identities do not. Restrict state administrators, retain bucket recovery
versions, and do not add automatic state/data deletion. Existing default Firestore
databases, Identity Platform configuration or repositories must be inspected and
imported before applying, never replaced blindly.

```sh
terraform -chdir=infra/terraform init -backend-config=bucket=STATE_BUCKET -backend-config=prefix=handoff-guardian/prototype
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
```

For credential-free syntax checks only use `init -backend=false` instead. Never
run `plan` with invented project inputs to simulate cloud verification.

The Google provider explicitly sets `billing_project` to the authorized
`project_id` and `user_project_override=true`. This attaches the target project
as the quota consumer for APIs such as Identity Platform, Firebase Rules and
Billing Budgets when using end-user credentials. The caller needs
`serviceusage.services.use` on that project. This quota-project setting does not
change the linked Cloud Billing account or grant billing/IAM permissions. In
particular, setting `gcloud config project` alone does not establish Terraform's
quota attribution. An ephemeral OAuth access token may be supplied using the
provider/backend's supported environment variables; never print it, commit it,
or put it into Terraform inputs or saved command files.

During the first authorized bootstrap of `qnova-ai-builder`, Identity Platform,
Firebase Rules and Billing Budgets failed without explicit quota attribution.
After applying these provider settings, the operator observed successful
creation of the Identity Platform configuration, deny-all Firestore Rules and
the project-filtered RM200 alert budget. This establishes the quota fix for that
bootstrap; it does not establish application or agent deployment success.

## Resource bootstrap and IAM

Copy `terraform.tfvars.example` to ignored `terraform.tfvars`, fill the actual
project/locations/model/version, and leave `deploy_services=false`. The operator
needs API/resource/IAM administration for this explicit resource inventory, not
a standing application reviewer role. Review the generated plan before apply:

```sh
terraform -chdir=infra/terraform plan -out=bootstrap.tfplan
terraform -chdir=infra/terraform show bootstrap.tfplan
terraform -chdir=infra/terraform apply bootstrap.tfplan
terraform -chdir=infra/terraform output -json
```

The stack defines separate web/API/worker/agent/Tasks/Scheduler/build identities;
private evidence, agent-staging and build-source buckets; Firestore Native mode
with deletion protection, deny-all browser rules and indexes; OCR processor
creation or selection; Identity Platform configuration/domains; immutable-tag
Artifact Registry; bounded queue/recovery; logging metrics and Monitoring alerts.
Secret Manager is enabled; only explicitly requested secret containers are
created. Add private versions through Secret Manager outside Terraform. Public
Firebase browser configuration is not a backend credential. No private value is
placed in Terraform variables or state. An optional project-filtered billing
budget requires the authorized billing account, currency and explicit amount.
Budget alerts do not cap spending. Notification channels must be verified; an
empty list creates policies without an email/Pub/Sub delivery destination.

Explicit `operator_members` can mint a short-lived OAuth access token for only
the API service account, using a custom role containing only
`iam.serviceAccounts.getAccessToken`. Reviewer bootstrap can impersonate that
attached identity when Firebase Admin rejects the standard gcloud user OAuth
client. This service-account binding grants neither project-wide impersonation
nor signing or delegation permissions. It permits the API identity's existing
privileges for the token lifetime, so keep `operator_members` limited to trusted
deployment operators and never persist or print the minted token.

The API/worker have database-scoped Firestore access and an evidence-bucket
custom role containing only object create/get. They cannot list, overwrite or
delete evidence objects. Application generation preconditions and current
project membership remain mandatory. Web has no domain data/model access.
Agent has Vertex prediction/logging but no evidence bucket or Firestore grant.
Worker may query/get only the actual configured agent resource. Tasks and
Scheduler can invoke only the worker. Build can push only the image repository,
read only the build-source bucket, and act as the four relevant runtime accounts.
Once the services exist, its get/update grants are bound to those exact services;
project-level `run.operations.get` permits rollout polling. It cannot create Run
services or change service IAM.

The agent deployer has only `aiplatform.reasoningEngines.create`, `.get`, `.update`
and `.list`. Its SDK polls named child long-running operations during deployment.
Google IAM has no `aiplatform.operations.get` permission; do not add that invalid
permission to a custom role. The broad predefined Agent Platform User role is
also unnecessary. Check actual create/update/polling against the dedicated build
identity during cloud verification rather than inferring deployment success from
Terraform syntax validation.

Budget alerts require separate quota and authorization checks. A budget filtered
to this one project can be managed with project permissions
`resourcemanager.projects.get`, `billing.resourceCosts.get`,
`billing.resourcebudgets.read` and `billing.resourcebudgets.write`, provided the
budget's ownership scope allows project users (`ALL_USERS` or unspecified).
`BILLING_ACCOUNT` ownership restricts mutation to authorized billing-account
users even when a project filter is present. Do not grant a runtime/build account
Billing Account Administrator to fix a quota-project error. If account-wide
budget access is unavailable, use the explicitly project-filtered budget and
review its ownership scope. Budget notification channels support email targets.
The stack enables project-level recipients for its single-project budget;
Google currently defines these recipients as users with the project's `Owner`
role. `Editor` users require an explicit email notification channel if they
should also receive alerts. Default billing-account IAM recipients remain
enabled. No spending-cap behavior is configured by this stack.

Document AI is not listed among supported IAM `resource.name` condition resource
types, and the Google provider does not supply a processor IAM resource. Its
worker custom role therefore grants only
`documentai.processorVersions.processOnline` at project scope. The configured
processor/version is pinned in the worker; there is no OCR list/write/batch
permission. This narrower permission scope is explicit rather than an
unsupported condition that silently denies every OCR call. Verify all runtime
IAM on the actual target during cloud acceptance.

Firestore conditional bindings accept the selected project ID/number and only
the `(default)` database. Client rules deny all direct browser reads/writes.
Backend SDKs bypass those rules and must authorize every project operation.
Collection-group membership lookup has an explicit `user_id` index; jobs/outbox
recovery indexes and unindexed long evidence fields are defined separately.

## Identity Platform email and password sign-in

The user selected email as the username with password sign-in. Terraform enables
Identity Platform's email provider with `password_required=true`; email-link,
anonymous and phone sign-in are disabled. The deployed API accepts only the
password sign-in provider and requires a verified email plus an operator-managed
UID mapping and project membership.

Register a Firebase web app in the authorized project and resolve its actual
public API key and app ID. Restrict the public key to the actual web origin and
required identity APIs. Terraform supplies the exact Run web hostname and any
explicitly reviewed extra hostnames as authorized domains. No Google OAuth
provider, consent-screen setup or client secret is required for this login flow.
Identity Platform stores and verifies passwords; passwords never enter Terraform
inputs, state, operator scripts or repository files.

The intended reviewer creates an account through the deployed app using their
own email and password, opens the verification email, then signs in again to
refresh the verified token. An operator runs reviewer bootstrap for that existing
verified account. Account creation alone grants no application project access;
the user must not share their password with the operator.

## Build, agent, and first application deployment

Cloud Build uses the dedicated `hg-build` service account with Cloud Logging only.
The authorized submitter needs `cloudbuild.builds.create`, source upload access,
and `iam.serviceAccounts.actAs` on that build identity. `operator_members` supplies
build-source uploader, bootstrap database and identity-read access; explicitly
grant submitter/build-account permissions through the deployment administrator
as needed. `.gcloudignore` excludes tools, credentials, state, tokens and local
build output while retaining tests/contracts/fixtures needed for offline checks.
Runtime Docker contexts and the agent source package exclude evaluation fixtures.

```sh
gcloud builds submit . --project=PROJECT_ID --config=cloudbuild.yaml --service-account=projects/PROJECT_ID/serviceAccounts/hg-build@PROJECT_ID.iam.gserviceaccount.com --gcs-source-staging-dir=gs://PROJECT_ID-hg-build-source/source --substitutions=_REGION=APPROVED_REGION
```

Default behavior is meaningful locked tests (including the checksum-verified
Firestore emulator), frontend browser tests, image builds and pushes. It does
not deploy the agent or application. Python dependencies install through `uv
sync --frozen`; frontend dependencies through `npm ci`. Source upload and all
Cloud Build checks are separate from real-service acceptance. Build logs emit
actual `artifacts/images.json` values with immutable image digests; Cloud Build
records image results. Copy those actual digests into the ignored Terraform
configuration. Preserve the successful build ID for deployment verification.

Prepare an agent manifest without spending or deploying:

```sh
uv --directory agent run python ../scripts/deploy_agent.py --project PROJECT_ID --location APPROVED_REGION --model ACTUAL_MODEL --staging-bucket PROJECT_ID-hg-agent-staging --service-account hg-agent@PROJECT_ID.iam.gserviceaccount.com
```

After deployment authorization, deploy through the same script with
`--allow-deploy --output ../artifacts/agent-deployment.json`, or submit Cloud
Build with `_DEPLOY_AGENT=true` and actual `_GEMINI_MODEL`/
`_AGENT_STAGING_BUCKET`. For every subsequent agent deployment pass the emitted
`--resource-name` / `_AGENT_RESOURCE`. Reconcile an ambiguous first create against
the actual inventory before retrying; create is not automatically exactly once.
Retain the actual resource, operation schemas and SDK/model versions.

Set `agent_engine_resource_name`, `api_image`, `web_image`, the two public identity
values, then `deploy_services=true` in the ignored configuration. Generate,
review and apply a second saved plan. Short `hg-*` names permit Google's
documented deterministic origins, allowing exact API CORS/web/worker audiences
without wildcard origins or a circular startup dependency. Verify Terraform's
expected origins against actual `gcloud run services describe` results.

```sh
terraform -chdir=infra/terraform plan -out=services.tfplan
terraform -chdir=infra/terraform show services.tfplan
terraform -chdir=infra/terraform apply services.tfplan
gcloud run services describe hg-web --project=PROJECT_ID --region=APPROVED_REGION --format='value(status.url)'
gcloud run services describe hg-api --project=PROJECT_ID --region=APPROVED_REGION --format='value(status.url)'
gcloud run services describe hg-worker --project=PROJECT_ID --region=APPROVED_REGION --format='value(status.url)'
```

Every service starts at zero minimum/two maximum instances. Worker concurrency
is 1, API 20, web 40. Tasks dispatches at 2/sec with maximum concurrency 2;
the application reserves at most three durable external attempts independently
of delivery retries. Scheduler sends `{}` to private recovery once a minute,
with its own service account and exact worker-origin OIDC audience. Worker
dispatch timeout is 300 seconds, lease 360, OCR deadline 120 and agent budget
60 seconds/at most three model requests. Retries can repeat paid OCR/inference.

For later image rollouts Cloud Build `_DEPLOY=true` updates only images on the
three already configured services. It preserves Terraform's identity, IAM, env,
limits and worker entry point. Supply actual `_AGENT_RESOURCE` and
`_GEMINI_MODEL`; synchronize Terraform image variables afterward so a later
apply does not restore old images. Agent deployment is a separate explicit flag.

## Trusted synthetic bootstrap and live acceptance

Create one selected synthetic project. Each intended reviewer/viewer/nonmember
creates their own email/password account in the app and verifies their email
before operator bootstrap. No fixture alias is a login credential:

```sh
uv --directory backend run python ../scripts/seed_demo.py --google-cloud-project PROJECT_ID --operator-id OPERATOR init --scenario scn_01
uv --directory backend run python ../scripts/bootstrap_reviewer.py --google-cloud-project PROJECT_ID --operator-id OPERATOR --project-id prj_harbour_portal --user-id usr_reviewer --email REAL_REVIEWER_EMAIL --gcloud-account DEPLOY_OPERATOR_EMAIL --impersonate-service-account hg-api@PROJECT_ID.iam.gserviceaccount.com
uv --directory backend run python ../scripts/bootstrap_reviewer.py --google-cloud-project PROJECT_ID --operator-id OPERATOR --project-id prj_harbour_portal --user-id usr_viewer --email REAL_VIEWER_EMAIL --role viewer --gcloud-account DEPLOY_OPERATOR_EMAIL --impersonate-service-account hg-api@PROJECT_ID.iam.gserviceaccount.com
```

The nonmember must sign in but has no membership in this project. Bootstrap
verifies the existing UID, verified email and password provider and audits the
explicit mapping.
Rebinding a UID/domain user to another account is rejected; role changes require
`--change-role`; `--revoke` disables a binding while retaining history.

Synthetic bootstrap initializes an empty project and exact capacity counters.
Upload/review evidence using the signed-in application; it never seeds accepted
authority or canned findings. Optional `propose-commitments` takes a bounded JSON
list of version-one **proposed** commitments and validates immutable runtime GCS
anchors before transactional creation. Do not copy Markdown offsets to PDFs.
Only the trusted script can advance time, with the current evidence revision:

```sh
uv --directory backend run python ../scripts/seed_demo.py --google-cloud-project PROJECT_ID --operator-id OPERATOR advance-clock --project-id prj_harbour_portal --as-of 2026-10-24T12:00:00Z --expected-evidence-revision ACTUAL_REVISION
```

This requires the internal synthetic marker, moves forward, increments evidence
revision and audits real UTC occurrence. Every old action binding becomes stale.

The acceptance runner supports every required case ID and safe JSON reports.
Without `--allow-live`, all cases are `not_run` and it returns 1. With missing
inputs, affected cases are `blocked` with their requirement. Real probes cover
deployment inventory/build digests, token rejection, viewer/nonmember checks,
worker rejection, generated-PDF ingestion/idempotency, live analysis/grounding,
and explicitly approved task execution/concurrency. Partial/manual observations
do not turn a case green: browser identity, isolated failure injection, real
queue/recovery, full semantic scoring and actual Firestore contention evidence
remain blocked until performed and recorded. The runner does not offer a
`force-pass` or import fabricated passing observations.

```sh
uv --directory backend run python ../scripts/run_cloud_acceptance.py --all --report ../artifacts/cloud-acceptance.json
uv --directory backend run python ../scripts/run_cloud_acceptance.py --case DEPLOY-01 --allow-live --google-cloud-project PROJECT_ID --region APPROVED_REGION --agent-resource ACTUAL_AGENT_RESOURCE --build-id ACTUAL_BUILD_ID --report ../artifacts/deployment-acceptance.json
```

For signed probes, place actual Identity Platform tokens from separate interactive
email/password sign-ins in a restricted directory **outside this repository**, named
`reviewer.token`, `viewer.token`, `nonmember.token`, plus actual `expired.token`
and `wrong-project.token` when those cases are selected. Never use service-account
tokens as users, print tokens, or pass their contents in command arguments. Use
owner-only `0600` permissions on Unix; on Windows restrict file ACLs to the
operator/SYSTEM/administrators and remove broad inherited Users/Everyone access.
The runner validates those restrictions and never writes tokens into reports.
Remove temporary token files through a separate bounded operator action after
the run. Supply `--api-url` as the origin **without** `/api/v1`, `--project-id`,
`--token-dir`, and actual resource IDs. `--help` lists optional existing PDF/run/
approved-action IDs and repeatable upload/analysis keys. Model/upload/task effects
are limited to an operator-marked synthetic project. The runner never assigns
source authority or approves an action for the reviewer.

## Rollback, pause and targeted cleanup

Keep prior successful digest/agent manifests. Roll back by setting the previously
verified image digests in Terraform and applying a reviewed plan. Re-deploy the
prior agent package to the same recorded resource with its prior pinned model/
dependency versions. Verify state compatibility before either rollback; this
does not rewind Firestore data or approve stale actions.

```sh
gcloud tasks queues pause handoff-guardian-work --project=PROJECT_ID --location=APPROVED_REGION
gcloud scheduler jobs pause handoff-guardian-recovery --project=PROJECT_ID --location=APPROVED_REGION
gcloud tasks queues resume handoff-guardian-work --project=PROJECT_ID --location=APPROVED_REGION
gcloud scheduler jobs resume handoff-guardian-recovery --project=PROJECT_ID --location=APPROVED_REGION
```

Pause both delivery and recovery for an intentional maintenance window. In-flight
work may still complete or be billed. Recovery reconciles durable jobs after
resuming. Cleanup requires authorization covering data destruction: inspect an
explicit target/resource inventory, preserve needed evidence/state, pause work,
then review a targeted Terraform destroy plan. Evidence `prevent_destroy`,
Firestore deletion protection/ABANDON and nonempty buckets intentionally block
automatic data teardown. Do not bypass those controls or run broad project/bucket
deletion as an ordinary rollback.

## Verified official interfaces

Observed deployment (2026-10-18): release build `a51accb5-f3d3-45f9-bfb2-27ed109783c9` and agent build `5143fc7d-f372-4e73-863b-13afa49c7387` succeeded in `qnova-ai-builder`. Actual service URLs/revisions/digests and acceptance are in `harness/build-log.md`; use existing runtime `projects/622303555908/locations/us-central1/reasoningEngines/1435140026055786496` on updates. Terraform final plan found no differences. Full signed-user/OCR/model/action acceptance remains incomplete.

Managed runtime deployment must omit reserved project/location environment names; the serialized application carries these values. The SDK must receive filtered pinned requirement entries, not annotated uv-export lines. Its automatic GenAI event telemetry is explicitly disabled with the supported `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=false` control, in addition to application tracing=false; source packets must not enter event logs. This documented control is an exception to the general instruction to avoid new names under the platform-owned prefix. Standard application status/usage logging remains enabled.

- [Google provider 8.7.0 registry](https://registry.terraform.io/providers/hashicorp/google/8.7.0/docs)
- [Google provider quota-project troubleshooting](https://registry.terraform.io/providers/hashicorp/google/latest/docs/guides/common_issues)
- [Billing budget permissions](https://docs.cloud.google.com/billing/docs/how-to/custom-roles)
- [Budget ownership scopes](https://docs.cloud.google.com/billing/docs/reference/budget/rest/v1/billingAccounts.budgets)
- [GCS state backend](https://developer.hashicorp.com/terraform/language/backend/gcs)
- [Cloud Run deterministic origins](https://docs.cloud.google.com/run/docs/triggering/https-request)
- [Cloud Tasks authenticated Run delivery](https://docs.cloud.google.com/run/docs/triggering/using-tasks)
- [Scheduler OIDC](https://docs.cloud.google.com/scheduler/docs/http-target-auth)
- [IAM supported resource condition attributes](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes)
- [Document AI permissions](https://docs.cloud.google.com/document-ai/docs/access-control/iam-permissions)
- [Agent runtime/service-agent setup](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/setup)
- [Identity Platform email/password sign-in](https://docs.cloud.google.com/identity-platform/docs/sign-in-user-email)
- [Cloud Build Run deployment](https://docs.cloud.google.com/build/docs/deploying-builds/deploy-cloud-run)
