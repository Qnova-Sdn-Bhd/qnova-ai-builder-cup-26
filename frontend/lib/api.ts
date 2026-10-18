import type { Action, Finding } from "./types";

export function findingStatusLabel(finding: Finding, actions: Action[]): string {
  const taskCreated = actions.some(action => action.payload.finding_id === finding.id && action.status === "executed" && Boolean(action.task_id));
  return finding.status === "open" && taskCreated ? "Task created · Awaiting resolution evidence" : label(finding.status);
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public requestId?: string) { super(message); }
}
export async function apiRequest<T>(base: string, path: string, token: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${base.replace(/\/$/, "")}${path}`, { ...init, headers, cache: "no-store", signal: init.signal ?? AbortSignal.timeout(45000) });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.error || body;
    throw new ApiError(response.status, detail.message || `Request failed (${response.status}).`, detail.request_id);
  }
  return response.json() as Promise<T>;
}
export function exactQuote(text: string, start: number, end: number): string { return Array.from(text).slice(start, end).join(""); }
export function uniqueQuote(text: string, quote: string): { start_char: number; end_char: number } {
  if (!quote.trim()) throw new Error("Enter an exact, nonempty passage.");
  const first = text.indexOf(quote);
  if (first < 0) throw new Error("That passage does not occur in the extracted source.");
  if (text.indexOf(quote, first + 1) >= 0) throw new Error("That passage occurs more than once. Select a longer unique passage.");
  const start_char = Array.from(text.slice(0, first)).length;
  return { start_char, end_char: start_char + Array.from(quote).length };
}
export function label(value: string) { return value.replaceAll("_", " ").replace(/^./, c => c.toUpperCase()); }
export function isFresh(action: { binding: { evidence_revision: number; base_board_revision: number; finding_version: number } }, evidenceRevision: number, boardRevision: number, findingVersion: number) {
  return action.binding.evidence_revision === evidenceRevision && action.binding.base_board_revision === boardRevision && action.binding.finding_version === findingVersion;
}
