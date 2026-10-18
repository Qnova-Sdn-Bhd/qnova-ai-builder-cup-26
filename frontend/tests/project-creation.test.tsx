import { beforeAll, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ProjectCreate } from "../components/guardian";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = vi.fn();
});

function setup(request = vi.fn().mockResolvedValue({ id: "new", name: "Delivery" })) {
  const created = vi.fn(); const close = vi.fn();
  render(<ProjectCreate reviewerProjectId="existing" defaultTimezone="Asia/Kuala_Lumpur" request={request} created={created} close={close} />);
  fireEvent.change(screen.getByLabelText("Project name"), { target: { value: " Delivery " } });
  return { request, created, close };
}

describe("reviewer project creation", () => {
  it("sends the selected reviewer project and opens only a confirmed saved project", async () => {
    const { request, created } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => expect(created).toHaveBeenCalledWith({ id: "new", name: "Delivery" }));
    const [path, init] = request.mock.calls[0];
    expect(path).toBe("/projects");
    expect(JSON.parse(init.body)).toEqual({ name: "Delivery", timezone: "Asia/Kuala_Lumpur", reviewer_project_id: "existing" });
    expect(init.headers["Idempotency-Key"]).toBeTruthy();
  });

  it("retains the same idempotency key after an ambiguous failure and blocks duplicate submits", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("Request timed out")).mockResolvedValue({ id: "saved" });
    const { created } = setup(request);
    const button = screen.getByRole("button", { name: "Create project" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(request).toHaveBeenCalledTimes(1);
    expect(created).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Request timed out");
    fireEvent.click(button);
    await waitFor(() => expect(created).toHaveBeenCalledWith({ id: "saved" }));
    expect(request.mock.calls[0][1].headers["Idempotency-Key"]).toBe(request.mock.calls[1][1].headers["Idempotency-Key"]);
  });

  it("rejects blank names and invalid timezones before submitting", async () => {
    const { request } = setup();
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a project name");
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Delivery" } });
    fireEvent.change(screen.getByLabelText(/^Project timezone/), { target: { value: "Invalid/Zone" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("valid IANA timezone");
    expect(request).not.toHaveBeenCalled();
  });

  it("lets the reviewer cancel without writing", () => {
    const { request, close } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(close).toHaveBeenCalledOnce();
    expect(request).not.toHaveBeenCalled();
  });
});

