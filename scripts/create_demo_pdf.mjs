// Build visibly synthetic PDF evidence. Document AI must extract it after normal upload.
import { createRequire } from "node:module";
import { readFile, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(resolve(root, "frontend/package.json"));
const { chromium } = require("@playwright/test");
const source = resolve(root, "fixtures/inputs/sources/prj_harbour_portal/sv_hp_sow_001.md");
const destination = resolve(root, process.argv[2] || "artifacts/synthetic-harbour-sow.pdf");
const text = await readFile(source, "utf8");
const escaped = text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.setContent(`<html lang="en"><head><meta charset="utf-8"><title>Synthetic Harbour scope</title>
    <style>body { font: 12px Arial, sans-serif; line-height: 1.6; color: #172b38; }
    h1 { font-size: 23px; } pre { white-space: pre-wrap; font: inherit; }
    .notice { padding: 12px; background: #edf5f2; border: 1px solid #96b5a9; }</style></head>
    <body><h1>Harbour Portal · Statement of work</h1><p class="notice">SYNTHETIC COMPETITION DATA.
    All organisations, people and business records are fictional.</p><pre>${escaped}</pre></body></html>`);
  await mkdir(dirname(destination), { recursive: true });
  await page.pdf({ path: destination, format: "A4", printBackground: true,
    margin: { top: "20mm", right: "18mm", bottom: "20mm", left: "18mm" } });
  console.log(JSON.stringify({ status: "generated", path: destination,
    notice: "Not processed. Upload via authenticated API; inspect Document AI text and derive fresh anchors." }));
} finally {
  await browser.close();
}
