// Read-only inspection. No credential submission, authenticated requests, or cloud writes.
import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const target = process.argv[2];
if (target !== "https://hg-web-622303555908.us-central1.run.app") {
  throw new Error("Pass the explicitly authorized deployed web origin.");
}
const output = resolve("../artifacts/deployed-browser");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const results = [];
try {
  for (const viewport of [{ name: "desktop", width: 1440, height: 1000 }, { name: "mobile", width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    const consoleErrors = [], pageErrors = [], failedRequests = [], errorResponses = [];
    const safeUrl = value => { const url = new URL(value); return `${url.origin}${url.pathname}`; };
    page.on("console", event => { if (event.type() === "error") consoleErrors.push(event.text()); });
    page.on("pageerror", error => pageErrors.push(error.message));
    page.on("requestfailed", request => failedRequests.push({ url: safeUrl(request.url()), error: request.failure()?.errorText }));
    page.on("response", response => { if (response.status() >= 400) errorResponses.push({ url: safeUrl(response.url()), status: response.status() }); });
    const response = await page.goto(target, { waitUntil: "networkidle", timeout: 60000 });
    const html = await response.text();
    const signIn = page.getByRole("button", { name: "Sign in", exact: true });
    await signIn.waitFor({ state: "visible", timeout: 15000 });
    const enabled = await signIn.isEnabled();
    const configResponse = await context.request.get(`${target}/api/config`);
    const publicConfig = await configResponse.json();
    const headers = await response.allHeaders();
    const result = {
      viewport: viewport.name, status: response.status(), finalUrl: page.url(), title: await page.title(),
      signInVisible: await signIn.isVisible(), signInEnabled: enabled,
      missingConfigNotice: await page.getByText("Cloud environment needs configuration").count() > 0,
      publicConfigStatus: configResponse.status(), publicApiBaseUrl: publicConfig.apiBaseUrl,
      publicConfigNoStore: configResponse.headers()["cache-control"]?.includes("no-store") ?? false,
      runtimeConfigComplete: enabled && html.includes("Identity Platform"),
      emailUsernameVisible: await page.getByLabel("Email / username").isVisible(),
      passwordVisible: await page.getByLabel("Password", { exact: true }).isVisible(),
      googleProviderButtonCount: await page.getByRole("button", { name: /Continue with Google/ }).count(),
      horizontalOverflow: await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      securityHeaders: Object.fromEntries(["x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"].map(key => [key, headers[key] || null])),
      consoleErrors, pageErrors, failedRequests, errorResponses,
      screenshot: resolve(output, `${viewport.name}.png`),
    };
    await page.screenshot({ path: result.screenshot, fullPage: true });
    await page.getByRole("button", { name: "Create an account", exact: true }).click();
    result.createAccountVisible = await page.getByRole("heading", { name: "Create your account" }).isVisible();
    result.createAccountEnabled = await page.getByRole("button", { name: "Create account", exact: true }).isEnabled();
    result.createPasswordAutocomplete = await page.getByLabel("Password", { exact: true }).getAttribute("autocomplete");
    result.createPasswordMinimum = await page.getByLabel("Password", { exact: true }).getAttribute("minlength");
    result.createAccountScreenshot = resolve(output, `${viewport.name}-create-account.png`);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: result.createAccountScreenshot, fullPage: true });
    await page.getByRole("button", { name: "Back to sign in" }).click();
    await page.getByRole("button", { name: "Forgot password?" }).click();
    result.resetVisible = await page.getByRole("heading", { name: "Reset your password" }).isVisible();
    result.resetEnabled = await page.getByRole("button", { name: "Send reset link" }).isEnabled();
    result.resetHasNoPasswordInput = await page.getByLabel("Password", { exact: true }).count() === 0;
    result.resetSkipLinkGeometry = await page.evaluate(() => {
      const element = document.querySelector(".skip");
      const rect = element.getBoundingClientRect();
      return { focused: element.matches(":focus"), top: rect.top, bottom: rect.bottom };
    });
    result.resetScreenshot = resolve(output, `${viewport.name}-reset-password.png`);
    // Full-page capture otherwise includes viewport-fixed elements above a scrolled viewport.
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: result.resetScreenshot, fullPage: true });
    results.push(result);
    await context.close();
  }
} finally { await browser.close(); }
const report = { target, observedAt: new Date().toISOString(), scope: "signed-out read-only browser inspection; no credentials submitted or account requests attempted", results };
await writeFile(resolve(output, "inspection.json"), JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
