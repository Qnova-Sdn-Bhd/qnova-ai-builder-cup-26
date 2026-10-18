"use client";

import { useState, type FormEvent } from "react";
import { createUserWithEmailAndPassword, getAuth, sendEmailVerification, sendPasswordResetEmail, signInWithEmailAndPassword, signOut, type User } from "firebase/auth";
import { ArrowLeft, ArrowRight, Check, Eye, EyeOff, LoaderCircle, Mail, RefreshCw, ShieldCheck } from "lucide-react";

type Mode = "signin" | "create" | "reset";
function friendlyError(error: unknown): string {
  const code = typeof error === "object" && error !== null && "code" in error ? String(error.code) : "";
  if (["auth/invalid-credential", "auth/user-not-found", "auth/wrong-password", "auth/invalid-login-credentials"].includes(code)) return "The email or password was not accepted. Check your details or reset your password.";
  if (code === "auth/email-already-in-use") return "This email cannot be used to create a new account. Try signing in or resetting your password.";
  if (code === "auth/invalid-email") return "Enter a valid email address as your username.";
  if (code === "auth/weak-password" || code === "auth/password-does-not-meet-requirements") return "Choose a stronger password that meets the account policy. Use at least 12 characters.";
  if (code === "auth/too-many-requests") return "Too many requests. Wait a little before trying again.";
  if (code === "auth/network-request-failed") return "The account service could not be reached. Check your connection and try again.";
  if (code === "auth/operation-not-allowed" || code === "auth/configuration-not-found") return "Email and password sign-in is not enabled in this environment yet. Contact the environment operator.";
  return "The account request did not complete. Please try again.";
}

export function IdentityPanel({ configured, ready, user, initializationError, onVerified }: { configured: boolean; ready: boolean; user: User | null; initializationError: string; onVerified: () => void }) {
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const disabled = !configured || !ready || busy || Boolean(initializationError);
  function changeMode(next: Mode) { setMode(next); setError(""); setNotice(""); setShowPassword(false); }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (disabled) return;
    const form = event.currentTarget; const data = new FormData(form);
    setBusy(true); setError(""); setNotice("");
    try {
      const auth = getAuth();
      if (mode === "reset") {
        await sendPasswordResetEmail(auth, email.trim());
        setNotice("If this email has an account, a password reset link has been sent. Check your inbox and spam folder.");
      } else if (mode === "create") {
        const credential = await createUserWithEmailAndPassword(auth, email.trim(), String(data.get("password") || ""));
        form.reset();
        try { await sendEmailVerification(credential.user); setNotice("Verification email sent. Open the link in your inbox, then return here."); }
        catch (verificationError) { setError(`Your account was created, but the verification email could not be sent. ${friendlyError(verificationError)} Use Resend verification below.`); }
      } else {
        await signInWithEmailAndPassword(auth, email.trim(), String(data.get("password") || ""));
        form.reset();
      }
    } catch (authError) { setError(friendlyError(authError)); }
    finally { setBusy(false); }
  }
  async function resend() {
    if (!user || busy) return; setBusy(true); setError(""); setNotice("");
    try { await sendEmailVerification(user); setNotice("Verification email sent. Check your inbox and spam folder."); }
    catch (authError) { setError(friendlyError(authError)); }
    finally { setBusy(false); }
  }
  async function checkVerification() {
    if (!user || busy) return; setBusy(true); setError(""); setNotice("");
    try {
      await user.reload();
      if (!user.emailVerified) { setNotice("Your email is not verified yet. Open the verification link, then check again."); return; }
      await user.getIdToken(true);
      onVerified();
    } catch (authError) { setError(friendlyError(authError)); }
    finally { setBusy(false); }
  }
  return <section className="signin-card" aria-labelledby="identity-title">
    <div className="signin-icon">{user ? <Mail size={28} /> : <ShieldCheck size={30} />}</div>
    <div className="eyebrow">YOUR REVIEW WORKSPACE</div>
    <h2 id="identity-title">{user ? "Verify your email" : mode === "create" ? "Create your account" : mode === "reset" ? "Reset your password" : "Welcome to Handoff Guardian"}</h2>
    <p>{user ? <>You’re signed in as <strong>{user.email}</strong>. Verify this email before opening your project workspace.</> : mode === "create" ? "Use your email as your username. You’ll verify it before accessing any project." : mode === "reset" ? "Enter your email username and we’ll send a secure reset link." : "Sign in with your email and password to review the evidence and move your project forward."}</p>
    {user ? <div className="identity-form">
      <div className="verification-steps"><span><Check size={14} />Account created or signed in</span><span><Mail size={14} />Open the verification link in your email</span><span><ShieldCheck size={14} />Return here to continue securely</span></div>
      <button className="button primary identity-submit" disabled={busy} onClick={() => void checkVerification()}>{busy ? <LoaderCircle size={17} className="spin" /> : <RefreshCw size={17} />}I’ve verified my email</button>
      <button className="button identity-submit" disabled={busy} onClick={() => void resend()}>Resend verification email</button>
      <button className="text-button" disabled={busy} onClick={() => void signOut(getAuth()).catch(authError => setError(friendlyError(authError)))}><ArrowLeft size={14} />Use another account</button>
    </div> : <form className="identity-form" onSubmit={submit}>
      <label className="field"><span>Email / username</span><input name="email" type="email" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={254} placeholder="you@company.com" value={email} onChange={event => setEmail(event.target.value)} disabled={busy} /></label>
      {mode !== "reset" && <label className="field"><span id="identity-password-label">Password</span><span className="password-input"><input aria-labelledby="identity-password-label" name="password" type={showPassword ? "text" : "password"} autoComplete={mode === "create" ? "new-password" : "current-password"} required minLength={mode === "create" ? 12 : 6} maxLength={4096} placeholder={mode === "create" ? "At least 12 characters" : "Enter your password"} disabled={busy} /><button type="button" className="icon-button" aria-label={showPassword ? "Hide password" : "Show password"} onClick={() => setShowPassword(value => !value)}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></span>{mode === "create" && <small>Use at least 12 characters. Never share your password in chat.</small>}</label>}
      {mode === "signin" && <button className="text-button forgot-password" type="button" onClick={() => changeMode("reset")}>Forgot password?</button>}
      <button className="button primary identity-submit" type="submit" disabled={disabled}>{busy ? <LoaderCircle size={18} className="spin" /> : mode === "reset" ? <Mail size={17} /> : <ArrowRight size={17} />}{mode === "create" ? "Create account" : mode === "reset" ? "Send reset link" : "Sign in"}</button>
      <div className="identity-switch">{mode === "signin" ? <>New to Handoff Guardian? <button type="button" className="text-button" onClick={() => changeMode("create")}>Create an account</button></> : <button type="button" className="text-button" onClick={() => changeMode("signin")}><ArrowLeft size={14} />Back to sign in</button>}</div>
    </form>}
    {!configured && <div role="status" className="notice warning"><strong>Cloud environment needs configuration</strong><p>Email sign-in and the project API are not connected yet. Ask the environment operator to finish the Google Cloud setup.</p></div>}
    {(initializationError || error) && <p className="identity-error error" role="alert">{initializationError || error}</p>}
    <div aria-live="polite" aria-atomic="true">{notice && <div className="notice success identity-notice">{notice}</div>}</div>
    <div className="signin-note"><ShieldCheck size={16} /><span>{user ? "Verifying your email confirms your identity. Your administrator manages project membership." : "Email and password are secured by Google Cloud Identity Platform. Project access is managed by your administrator."}</span></div>
    <div className="signin-footer"><span>Google Cloud</span><span>Identity Platform</span></div>
  </section>;
}
