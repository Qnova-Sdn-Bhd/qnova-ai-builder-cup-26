import { expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { TaskCard } from "../components/guardian";
import type { Task, Workspace } from "../lib/types";

const task: Task = { id: "t1", title: "Review evidence", description: "Check the test report", owner_id: "u1", due_date: null, status: "open", version: 3, action_proposal_id: null };
const workspace = { project: { board_revision: 4 }, users: [{ id: "u1", display_name: "Reviewer" }], action_proposals: [] } as unknown as Workspace;

it("sends current task and board versions and waits for server state", () => {
  const mutate = vi.fn().mockResolvedValue(null);
  render(<TaskCard task={task} workspace={workspace} reviewer busy={false} mutate={mutate} />);
  fireEvent.click(screen.getByRole("button", { name: "Mark done: Review evidence" }));
  expect(mutate).toHaveBeenCalledWith("/tasks/t1/complete", { expected_task_version: 3, expected_board_revision: 4 }, expect.any(String));
  expect(screen.getByText("Open")).toBeInTheDocument();
  expect(screen.queryByText("Done")).not.toBeInTheDocument();
});

it("shows persisted done state without completion controls", () => {
  render(<TaskCard task={{ ...task, status: "done", version: 4 }} workspace={workspace} reviewer busy={false} mutate={vi.fn()} />);
  expect(screen.getByText("Done")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("hides completion from viewers", () => {
  render(<TaskCard task={task} workspace={workspace} reviewer={false} busy={false} mutate={vi.fn()} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("disables completion while saving", () => {
  render(<TaskCard task={task} workspace={workspace} reviewer busy mutate={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Mark done: Review evidence" })).toBeDisabled();
});
