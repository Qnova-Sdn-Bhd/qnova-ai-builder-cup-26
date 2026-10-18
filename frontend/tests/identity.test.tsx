import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { User } from "firebase/auth";
import { IdentityPanel } from "../components/identity-panel";
import { Guardian } from "../components/guardian";

const sdk = vi.hoisted(() => ({
  signIn: vi.fn(), create: vi.fn(), verify: vi.fn(), reset: vi.fn(), signOut: vi.fn(),
  session: null as unknown,
}));
vi.mock("firebase/app", () => ({ getApps: () => [], initializeApp: () => ({}) }));
vi.mock("firebase/auth", () => ({
  getAuth: () => ({}),
  onAuthStateChanged: (_auth: unknown, callback: (user: unknown) => void) => { callback(sdk.session); return () => {}; },
  signInWithEmailAndPassword: sdk.signIn, createUserWithEmailAndPassword: sdk.create,
  sendEmailVerification: sdk.verify, sendPasswordResetEmail: sdk.reset, signOut: sdk.signOut,
}));
const config = { apiBaseUrl: "https://api.run.app/api/v1", apiKey: "public-test-key", authDomain: "test.firebaseapp.com", projectId: "test", appId: "test-app" };
beforeEach(() => { vi.clearAllMocks(); sdk.session = null; sdk.signIn.mockResolvedValue({}); sdk.verify.mockResolvedValue(undefined); sdk.reset.mockResolvedValue(undefined); });
function panel(user: User | null = null) {
  const verified = vi.fn();
  render(<IdentityPanel configured ready user={user} initializationError="" onVerified={verified} />);
  return verified;
}
function credentials() {
  fireEvent.change(screen.getByLabelText("Email / username"), { target: { value: "reviewer@example.com" } });
  fireEvent.change(screen.getByLabelText("Password", { exact: true }), { target: { value: "synthetic-test-password" } });
}
describe("email and password identity", () => {
  it("submits credentials exclusively through the Firebase SDK and clears the password on success", async () => {
    panel(); credentials(); fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(sdk.signIn).toHaveBeenCalledWith({}, "reviewer@example.com", "synthetic-test-password"));
    await waitFor(() => expect(screen.getByLabelText("Password", { exact: true })).toHaveValue(""));
    expect(sdk.create).not.toHaveBeenCalled();
  });
  it("creates an account and sends an email verification without assigning a role", async () => {
    const user = { email: "reviewer@example.com" } as User; sdk.create.mockResolvedValue({ user });
    panel(); fireEvent.click(screen.getByRole("button", { name: "Create an account" })); credentials();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(sdk.create).toHaveBeenCalledWith({}, "reviewer@example.com", "synthetic-test-password"));
    expect(sdk.verify).toHaveBeenCalledWith(user);
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
  it("provides a neutral password-reset confirmation", async () => {
    panel(); fireEvent.change(screen.getByLabelText("Email / username"), { target: { value: "reviewer@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    fireEvent.click(screen.getByRole("button", { name: "Send reset link" }));
    await waitFor(() => expect(sdk.reset).toHaveBeenCalledWith({}, "reviewer@example.com"));
    expect(await screen.findByText(/If this email has an account/)).toBeInTheDocument();
  });
  it("resends verification and refuses continuation while still unverified", async () => {
    const user = { email: "reviewer@example.com", emailVerified: false, reload: vi.fn().mockResolvedValue(undefined), getIdToken: vi.fn() } as unknown as User;
    const verified = panel(user); fireEvent.click(screen.getByRole("button", { name: "Resend verification email" }));
    await waitFor(() => expect(sdk.verify).toHaveBeenCalledWith(user));
    fireEvent.click(screen.getByRole("button", { name: "I’ve verified my email" }));
    await waitFor(() => expect(user.reload).toHaveBeenCalled());
    expect(await screen.findByText(/Your email is not verified yet/)).toBeInTheDocument();
    expect(verified).not.toHaveBeenCalled(); expect(user.getIdToken).not.toHaveBeenCalled();
  });
  it("reloads verification and refreshes the ID token before continuing", async () => {
    const user = { email: "reviewer@example.com", emailVerified: false, reload: vi.fn(), getIdToken: vi.fn().mockResolvedValue("verified-test-token") };
    user.reload.mockImplementation(async () => { user.emailVerified = true; });
    const verified = panel(user as unknown as User);
    fireEvent.click(screen.getByRole("button", { name: "I’ve verified my email" }));
    await waitFor(() => expect(verified).toHaveBeenCalledOnce());
    expect(user.getIdToken).toHaveBeenCalledWith(true);
  });
  it("never requests a workspace for an unverified Firebase session", async () => {
    sdk.session = { email: "reviewer@example.com", emailVerified: false };
    const fetcher = vi.spyOn(globalThis, "fetch");
    render(<Guardian config={config} />);
    expect(await screen.findByRole("heading", { name: "Verify your email" })).toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled(); fetcher.mockRestore();
  });
  it("shows safe authentication errors without repeating submitted passwords", async () => {
    sdk.signIn.mockRejectedValue({ code: "auth/invalid-credential", message: "raw private test detail" });
    panel(); credentials(); fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("email or password was not accepted");
    expect(screen.queryByText("raw private test detail")).not.toBeInTheDocument();
  });
});
