// @vitest-environment node
import { spawnSync } from "node:child_process";
import { describe, it, expect } from "vitest";
const baseline = { NEXT_PUBLIC_API_BASE_URL: "https://api-123.run.app/api/v1", NEXT_PUBLIC_FIREBASE_API_KEY: "public-test-key", NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN: "project.firebaseapp.com", NEXT_PUBLIC_FIREBASE_PROJECT_ID: "project", NEXT_PUBLIC_FIREBASE_APP_ID: "app" };
function launch(overrides: Record<string, string>) { return spawnSync(process.execPath, ["scripts/start.mjs"], { env: { NODE_ENV: "production", PATH: process.env.PATH, ...baseline, ...overrides }, encoding: "utf8" }); }
describe("deployed frontend startup", () => {
  it("rejects missing Google configuration", () => { const result = launch({ NEXT_PUBLIC_FIREBASE_PROJECT_ID: "" }); expect(result.status).toBe(1); expect(result.stderr).toContain("Missing Google Cloud frontend configuration"); });
  it("rejects local APIs", () => { const result = launch({ NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000/api/v1" }); expect(result.status).toBe(1); expect(result.stderr).toContain("HTTPS Cloud Run API"); });
  it("rejects emulator settings", () => { const result = launch({ FIREBASE_AUTH_EMULATOR_HOST: "127.0.0.1:9099" }); expect(result.status).toBe(1); expect(result.stderr).toContain("Emulator settings are prohibited"); });
  it("rejects local identity and mock inference", () => { expect(launch({ AUTH_MODE: "local_demo" }).stderr).toContain("AUTH_MODE must be identity_platform"); expect(launch({ AI_PROVIDER: "mock" }).stderr).toContain("AI_PROVIDER must be vertex"); });
});
