# Handoff Guardian — Reviewer web application

**Technical review guide for the AI Builder Cup submission.** Start with the [project overview and judge walkthrough](../README.md), or [open the deployed prototype](https://hg-web-622303555908.us-central1.run.app).

This Next.js App Router application uses React and TypeScript and is deployed on Google Cloud Run. It helps a reviewer inspect evidence, understand cited AI findings and decide whether to create a specific blocker task.

## What judges can review

| Area | Experience |
| --- | --- |
| Account access | Email/password registration, verification, sign-in and password reset through Identity Platform |
| Project overview | Latest assessment state, warnings, findings and proposed/accepted commitments |
| Sources | Upload, processing state, original/canonical evidence and explicit source-authority review |
| Finding review | Explanations with exact cited passages, source versions and PDF page context when available |
| Action review | Exact task payload and binding; approval followed by separate task execution |
| Project board and audit | Persisted tasks and recorded review/action decisions |

Judges sign in with the pre-registered account supplied in the submission presentation deck; no registration is needed for the judge walkthrough. The submitter prepares email verification and reviewer membership in advance. Use the [ready-to-upload demo packet](../fixtures/README.md#judge-demo-use-the-ready-to-upload-files) in the assigned project. Actual passwords are not included in this README, and fixture email addresses are not login credentials. A viewer can inspect authorized information; a reviewer can request analysis and make privileged decisions. Self-registration remains an application capability for other users and does not grant project access or a role.

## Review behavior to look for

- Source-reported completion stays separate from reviewed acceptance.
- Failed, pending and stale assessments remain visible; earlier findings do not silently disappear.
- Source authority is assigned by a reviewer, not inferred from upload order or model confidence.
- `Approve exact proposal` records approval without creating a task. `Create approved task` executes separately.
- Changed evidence, board or finding bindings invalidate pending actions.
- A created task does not close a finding or commitment.
- The fictional-data label and simulated business date distinguish the synthetic demonstration from real project status.

The API enforces permissions and freshness. UI controls support the review flow but are not the authorization boundary. There is no browser role selector, fixture-backed workspace or model-provider switch.

## Implementation to inspect

| Location | Review focus |
| --- | --- |
| [components/guardian.tsx](components/guardian.tsx) | Workspace navigation, source/finding review, proposal approval/execution and task board |
| [components/identity-panel.tsx](components/identity-panel.tsx) | Account creation, verification, sign-in and reset |
| [lib/api.ts](lib/api.ts) / [lib/types.ts](lib/types.ts) | Authenticated API requests, exact evidence helpers and client types |
| [app/](app/) | App Router entry points, layout and styling |
| [scripts/start.mjs](scripts/start.mjs) / [Dockerfile](Dockerfile) | Protected Cloud Run startup and container packaging |
| [tests/](tests/) / [e2e/](e2e/) | Unit/component and desktop/mobile browser checks |

## Local setup and checks

Use Node.js 24 and the supplied [npm lockfile](package-lock.json). Run from the repository root; on Windows use `npm.cmd` if the PowerShell wrapper is blocked.

~~~sh
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build
~~~

For browser checks, after the production build:

~~~sh
cd frontend
npx --no-install playwright install chromium
npm run test:e2e
~~~

Playwright starts the production server and checks desktop/mobile entry behavior. These tests do not establish successful real-account login or a complete cloud review/task journey.

For an authorized local preview, set these public browser values from [the root example](../.env.example) in `frontend/.env.local`:

| Variable | Purpose |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | HTTPS Cloud Run API URL ending in `/api/v1` |
| `NEXT_PUBLIC_FIREBASE_API_KEY` | Public Identity Platform web API key |
| `NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN` | Firebase authentication domain |
| `NEXT_PUBLIC_FIREBASE_PROJECT_ID` | Identity project |
| `NEXT_PUBLIC_FIREBASE_APP_ID` | Registered Firebase web app ID |

From the root, run `npm --prefix frontend run dev`. Missing configuration displays an explicit setup state; it does not enable fake sign-in. Actual project access needs the configured Google Cloud API and server-provisioned membership.

These values are public application configuration, not backend credentials. The Cloud Run container startup validates configuration and rejects emulator endpoints or incompatible runtime modes. Passwords go directly to Identity Platform through its official Firebase client SDK; they are not sent to the application API.

## Recorded evidence and reproduction limits

The application release recorded **22 passing frontend unit tests and six browser checks**. Deployed sign-in, create-account and reset navigation were inspected on desktop and mobile. The reviewer subsequently verified their email and uploaded evidence, but a complete signed-in assessment, approval and persisted-task browser walkthrough remains unverified in the recorded acceptance evidence.

The frontend source, npm lockfile and local startup scripts are included. The separate [reviewer bootstrap helper](../scripts/bootstrap_reviewer.py) is also included but requires an authorized operator and an already verified account. Local UI tests do not establish deployed membership, successful analysis or reviewed task persistence. The original starter-pack documents needed by the full Cloud Build artifact validator are absent; see the [root reproduction limits](../README.md#reproduction-limitations-in-this-checkout).
