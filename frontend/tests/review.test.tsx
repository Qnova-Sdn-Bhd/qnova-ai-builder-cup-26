import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ActionReview, Guardian } from "../components/guardian";
import { apiRequest, ApiError, exactQuote, uniqueQuote } from "../lib/api";
import type { Action, Finding, Workspace } from "../lib/types";

// Test-only data: never imported by the application or container runtime.
const finding = { id: "f1", analysis_run_id: "run1", title: "Acceptance not demonstrated", status: "open", version: 2 } as Finding;
const action = { id: "a1", project_id: "p1", analysis_run_id: "run1", type: "create_blocker_task", status: "proposed", payload: { finding_id: "f1", title: "Review acceptance evidence", description: "Collect and review test results.", owner_id: "u1", due_date: null }, binding: { payload_hash: "abc", evidence_revision: 3, base_board_revision: 2, finding_version: 2 }, approval: null, task_id: null } as Action;
const workspace = { project: { name: "Test project", evidence_revision: 3, board_revision: 2 }, users: [{ id: "u1", display_name: "Test reviewer" }], analysis_runs: [{ id: "run1", status: "succeeded" }] } as Workspace;
function review(a = action, w = workspace, reviewer = true) {
  const mutate = vi.fn().mockResolvedValue(null); const openBoard = vi.fn();
  render(<ActionReview action={a} finding={finding} workspace={w} reviewer={reviewer} busy={false} mutate={mutate} openBoard={openBoard} revise={vi.fn()} />);
  return { mutate, openBoard };
}
describe("evidence code points", () => {
  it("counts astral characters as one code point", () => { expect(uniqueQuote("😀 before delivery evidence", "delivery evidence")).toEqual({ start_char: 9, end_char: 26 }); expect(exactQuote("A😀B", 1, 2)).toBe("😀"); });
  it("rejects absent and ambiguous quotes", () => { expect(() => uniqueQuote("same same", "same")).toThrow("more than once"); expect(() => uniqueQuote("actual", "invented")).toThrow("does not occur"); expect(() => uniqueQuote("actual", "")).toThrow("nonempty"); });
});
describe("exact action review", () => {
  it("approves the exact binding separately from execution", () => { const { mutate } = review(); fireEvent.change(screen.getByLabelText("Review note"), { target: { value: "Inspected exact payload" } }); fireEvent.click(screen.getByText("Approve exact proposal")); expect(mutate).toHaveBeenCalledWith("/action-proposals/a1/review", { decision: "approve", note: "Inspected exact payload", binding: action.binding }, expect.any(String)); expect(screen.queryByText("Create approved task")).not.toBeInTheDocument(); expect(screen.getByText("No due date proposed")).toBeInTheDocument(); });
  it("hides privileged controls from viewers", () => { review(action, workspace, false); expect(screen.queryByText("Approve exact proposal")).not.toBeInTheDocument(); expect(screen.queryByText("Reject")).not.toBeInTheDocument(); });
  it.each(["evidence_revision", "board_revision"])("blocks approval after %s changes", field => { review(action, { ...workspace, project: { ...workspace.project, [field]: 99 } }); expect(screen.getByText("Stale")).toBeInTheDocument(); expect(screen.queryByText("Approve exact proposal")).not.toBeInTheDocument(); });
  it("blocks execution after finding version changes", () => { review({ ...action, status: "approved", binding: { ...action.binding, finding_version: 1 } }); expect(screen.queryByText("Create approved task")).not.toBeInTheDocument(); expect(screen.getByText("Stale")).toBeInTheDocument(); });
  it("requires a successful server result before showing a persisted task", () => { const { mutate } = review({ ...action, status: "approved" }); fireEvent.click(screen.getByText("Create approved task")); expect(mutate).toHaveBeenCalledWith("/action-proposals/a1/execute", { binding: action.binding }, expect.any(String)); expect(screen.queryByText("Open task")).not.toBeInTheDocument(); });
  it("keeps an executed task visible after newer evidence revisions", () => { const { openBoard } = review({ ...action, status: "executed", task_id: "persisted-task" }, { ...workspace, project: { ...workspace.project, evidence_revision: 99 } }); expect(screen.getByText(/persisted-task/)).toBeInTheDocument(); fireEvent.click(screen.getByText("Open task")); expect(openBoard).toHaveBeenCalled(); expect(screen.queryByText("Stale")).not.toBeInTheDocument(); });
});
describe("honest sign-in", () => {
  it("shows missing Google Cloud configuration and no identity bypass", () => { render(<Guardian config={{ apiBaseUrl: "", apiKey: "", authDomain: "", projectId: "", appId: "" }} />); expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled(); expect(screen.getByText("Cloud environment needs configuration")).toBeInTheDocument(); expect(screen.queryByRole("combobox")).not.toBeInTheDocument(); expect(screen.queryByRole("button", { name: /Continue with Google/ })).not.toBeInTheDocument(); });
});
describe("API boundary", () => {
  it("sends only the Firebase bearer token and respects server conflicts", async () => { const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ error: { message: "Evidence changed", request_id: "req1" } }), { status: 409 })); const promise = apiRequest("https://api.run.app/api/v1", "/projects/p1/workspace", "firebase-token"); await expect(promise).rejects.toBeInstanceOf(ApiError); const init = fetcher.mock.calls[0][1]!; expect(new Headers(init.headers).get("Authorization")).toBe("Bearer firebase-token"); expect(new Headers(init.headers).has("X-Demo-Actor-Id")).toBe(false); expect(init.cache).toBe("no-store"); fetcher.mockRestore(); });
});
