// Capture Marketplace screenshots and GIF frames of the UI, using headless
// Microsoft Edge (or Chrome) over the DevTools protocol. No npm dependencies.
//
// 1. python tools/demo_history.py DEMO/claude
// 2. CLAUDE_CONFIG_DIR=DEMO/claude CLAUDE_HISTORY_DATA=DEMO/data python cch.py serve --port 8798
// 3. node tools/capture_media.mjs http://127.0.0.1:8798 docs/media [frames_dir]
//
// The page runs with a stub of the VS Code bridge (window.cchHost) so it shows
// the extension's buttons ("Continue in chat", "Send to chat") as users see them.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const [base = "http://127.0.0.1:8798", outDir = "docs/media", framesDir] = process.argv.slice(2);
const W = 1360, H = 820, PORT = 9333;
const browser = [
  process.env.BROWSER,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
].find(p => p && fs.existsSync(p));
if (!browser) throw new Error("No Edge/Chrome found; set BROWSER");

const sleep = ms => new Promise(r => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cch-capture-"));
const proc = spawn(browser, [`--headless=new`, `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--hide-scrollbars", "--force-dark-mode", "--no-first-run", "about:blank"], { stdio: "ignore" });

let target;
for (let i = 0; i < 50 && !target; i++) {
  try { target = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(t => t.type === "page"); }
  catch { await sleep(200); }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r, { once: true }));
let seq = 0;
const pending = new Map();
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
});
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expr => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true })).result.value;

await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
await send("Page.addScriptToEvaluateOnNewDocument", { source: `
  window.cchHost = {
    initialQuery: null,
    api: (p, body) => fetch(p, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) }).then(async r => { const d = await r.json(); if (!r.ok) throw new Error(d.error); return d; }),
    copy: async () => {}, open: async () => {}, resume: async () => "chat", insert: async () => {},
    saveAs: async () => {}, toChat: async () => {},
  };` });

async function go(url, settle = 1600) {
  await send("Page.navigate", { url: base + url });
  await sleep(settle);
}
async function shot(file) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, Buffer.from(data, "base64"));
  console.log("wrote", file);
}
const api = async p => (await fetch(base + p)).json();

// Ids from the demo data, looked up rather than hard-coded.
const sessions = (await api("/api/sessions?page_size=50")).sessions;
const byTitle = t => sessions.find(s => s.title.startsWith(t)).id;
const webhook = byTitle("Stripe webhook"), migration = byTitle("Alembic migration");
const hits = (await api("/api/search?q=retry%20backoff&page_size=5")).results;

// 1. History: sessions from every project, one open.
await go(`/?session=${webhook}`, 2200);
await shot(path.join(outDir, "history.png"));

// 2. Search across projects, with the matching message in context.
await go(`/?q=${encodeURIComponent("retry backoff")}`, 2400);
await shot(path.join(outDir, "search.png"));

// 3. Sessions containing matches, ranked, with counts.
await go(`/?q=${encodeURIComponent("migration OR retry OR idempotency")}&view=sessions&session=${migration}`, 2400);
await shot(path.join(outDir, "sessions-matches.png"));

// 4. Context pack built from two projects.
await go("/?tab=pack", 600);
await evaluate(`localStorage.setItem("cch.pack", JSON.stringify(${JSON.stringify(hits.slice(0, 2).map(h => ({
  message_id: h.id, before: 2, after: 2, role: h.role, ts: h.ts, project: h.project, title: h.title,
  preview: h.snippet.replace(/[\x02\x03]/g, "").slice(0, 140) })))}))`);
await go("/?tab=pack", 1200);
await evaluate(`document.querySelector("#pTask").value = "Add retries to the invoice export job"; document.querySelector("#pBuild").click()`);
await sleep(1500);
await shot(path.join(outDir, "context-pack.png"));
await evaluate(`localStorage.removeItem("cch.pack")`);

// 5. Archive / delete: bulk selection.
await go("/", 1600);
await evaluate(`[...document.querySelectorAll("#sessions .pick")].slice(1, 3).forEach(b => { b.checked = true; b.dispatchEvent(new Event("change")); })`);
await sleep(400);
await shot(path.join(outDir, "archive.png"));

// GIF frames: type a search, results appear, open the second hit.
if (framesDir) {
  fs.rmSync(framesDir, { recursive: true, force: true });
  let n = 0;
  const frame = async (hold = 1) => { for (let i = 0; i < hold; i++) await shot(path.join(framesDir, `f${String(n++).padStart(3, "0")}.png`)); };
  await go("/", 1800);
  await frame(4);
  const query = "retry backoff";
  for (let i = 1; i <= query.length; i++) {
    await evaluate(`(() => { const q = document.querySelector("#q"); q.focus(); q.value = ${JSON.stringify(query.slice(0, i))};
      q.dispatchEvent(new Event("input")); })()`);
    await sleep(110);
    await frame();
  }
  await sleep(900);
  await frame(6);
  await evaluate(`document.querySelectorAll("#results .item")[1]?.click()`);
  await sleep(1200);
  await frame(8);
  await evaluate(`document.querySelector(".seg [data-view=sessions]").click()`);
  await sleep(1200);
  await frame(8);
}

ws.close();
proc.kill();
await sleep(500);
fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
