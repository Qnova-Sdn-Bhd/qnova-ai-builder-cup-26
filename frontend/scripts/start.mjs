const required = ["NEXT_PUBLIC_API_BASE_URL", "NEXT_PUBLIC_FIREBASE_API_KEY", "NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN", "NEXT_PUBLIC_FIREBASE_PROJECT_ID", "NEXT_PUBLIC_FIREBASE_APP_ID"];
const missing = required.filter(name => !process.env[name]);
if (missing.length) throw new Error(`Missing Google Cloud frontend configuration: ${missing.join(", ")}`);
const api = new URL(process.env.NEXT_PUBLIC_API_BASE_URL);
if (api.protocol !== "https:" || !api.hostname.endsWith(".run.app") || !api.pathname.endsWith("/api/v1") || api.username || api.password) throw new Error("NEXT_PUBLIC_API_BASE_URL must be the HTTPS Cloud Run API URL ending in /api/v1.");
for (const key of Object.keys(process.env)) {
  if (key.includes("EMULATOR") && process.env[key]) throw new Error("Emulator settings are prohibited in deployed startup.");
}
for (const [name, expected] of Object.entries({ AUTH_MODE: "identity_platform", AI_PROVIDER: "vertex", AGENT_RUNTIME: "vertex_agent_engine", STORAGE_BACKEND: "firestore", FILE_STORAGE_BACKEND: "gcs", QUEUE_BACKEND: "cloud_tasks", TASK_BOARD_BACKEND: "firestore" })) {
  if (process.env[name] && process.env[name] !== expected) throw new Error(`${name} must be ${expected}.`);
}
await import("./server.js");
