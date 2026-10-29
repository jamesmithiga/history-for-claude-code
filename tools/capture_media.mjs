// Record the Marketplace walkthrough GIFs with headless Microsoft Edge (or
// Chrome) over the DevTools protocol. No npm dependencies.
//
// 1. python tools/demo_history.py DEMO/claude            (fictional history)
// 2. CLAUDE_CONFIG_DIR=DEMO/claude CLAUDE_HISTORY_DATA=DEMO/data python cch.py serve --port 8798
// 3. node tools/capture_media.mjs http://127.0.0.1:8798 docs/media FRAMES_DIR [scenario ...]
//
// Real mouse events drive the page (so :hover states work) and a drawn cursor
// with a click ripple shows viewers where to click. Each scenario's frames go
// to FRAMES_DIR/<name>/ and become docs/media/<name>.gif via tools/make_gif.py.
// The page runs with a stub of the VS Code bridge so it shows the extension's
// buttons ("Continue in chat", "Send to chat").
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const [base = "http://127.0.0.1:8798", outDir = "docs/media", framesRoot = path.join(os.tmpdir(), "cch-frames"),
  ...only] = process.argv.slice(2);
const W = 1360, H = 820, PORT = 9333, FRAME_MS = 120;
const here = path.dirname(fileURLToPath(import.meta.url));
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
const proc = spawn(browser, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
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
    copy: async () => {}, open: async () => {}, resume: async (id, mode) => mode, insert: async () => {},
    saveAs: async () => {}, toChat: async () => {},
  };
  // Drawn cursor + click ripple (headless screenshots have no pointer).
  window.__cursor = (() => {
    const c = document.createElement("div");
    c.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M4 2l6.5 18 2.6-7.4L20.5 10z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, { position: "fixed", left: "-50px", top: "-50px", zIndex: 2147483647, pointerEvents: "none",
      filter: "drop-shadow(0 2px 3px rgba(0,0,0,.55))" });
    const mount = () => document.body.appendChild(c);
    document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", mount) : mount();
    return {
      at(x, y) { c.style.left = (x - 3) + "px"; c.style.top = (y - 2) + "px"; },
      ripple(x, y) {
        const r = document.createElement("div");
        Object.assign(r.style, { position: "fixed", left: (x - 16) + "px", top: (y - 16) + "px", width: "32px", height: "32px",
          borderRadius: "50%", background: "rgba(224,122,82,.35)", border: "2px solid #e07a52", zIndex: 2147483646,
          pointerEvents: "none", transition: "transform .5s ease-out, opacity .5s ease-out" });
        document.body.appendChild(r);
        requestAnimationFrame(() => requestAnimationFrame(() => { r.style.transform = "scale(1.9)"; r.style.opacity = "0"; }));
        setTimeout(() => r.remove(), 700);
      },
    };
  })();` });

const api = async p => (await fetch(base + p)).json();
let mouse = { x: W * 0.55, y: H * 0.45 };
let frameDir = null, frameNo = 0;

async function snap() {
  if (!frameDir) return;
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync(path.join(frameDir, `f${String(frameNo++).padStart(4, "0")}.png`), Buffer.from(data, "base64"));
}
async function hold(ms) {
  const end = Date.now() + ms;
  do { const t = Date.now(); await snap(); await sleep(Math.max(0, FRAME_MS - (Date.now() - t))); } while (Date.now() < end);
}
async function go(url, settle = 1500) {
  await send("Page.navigate", { url: base + url });
  await sleep(settle);
  await evaluate(`window.__cursor.at(${mouse.x}, ${mouse.y})`);
}
async function center(selector) {
  const r = await evaluate(`(() => { const el = ${selector.startsWith("(") ? selector : `document.querySelector(${JSON.stringify(selector)})`};
    if (!el) return null; el.scrollIntoView({ block: "nearest" }); const b = el.getBoundingClientRect();
    return { x: b.left + Math.min(b.width / 2, 60), y: b.top + b.height / 2 }; })()`);
  if (!r) throw new Error(`not found: ${selector}`);
  return r;
}
async function moveTo(selector, steps = 9) {
  const to = await center(selector), from = { ...mouse };
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, e = t < .5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;  // ease in-out
    mouse = { x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e };
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: mouse.x, y: mouse.y });
    await evaluate(`window.__cursor.at(${mouse.x}, ${mouse.y})`);
    await snap();
  }
}
async function click(selector, after = 700) {
  await moveTo(selector);
  await hold(160);
  await evaluate(`window.__cursor.ripple(${mouse.x}, ${mouse.y})`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: mouse.x, y: mouse.y, button: "left", clickCount: 1 });
  }
  await hold(after);
}
async function type(text, perChar = 70) {
  for (const ch of text) { await send("Input.insertText", { text: ch }); await sleep(perChar); await snap(); }
}
async function record(name, fn) {
  if (only.length && !only.includes(name)) return;
  frameDir = path.join(framesRoot, name);
  fs.rmSync(frameDir, { recursive: true, force: true });
  fs.mkdirSync(frameDir, { recursive: true });
  frameNo = 0;
  // Start every recording from default UI preferences (Details closed, empty pack, default split).
  await go("/", 500);
  await evaluate(`["cch.details", "cch.pack", "cch.listw", "cch.filters"].forEach(k => localStorage.removeItem(k))`);
  await fn();
  frameDir = null;
  const archived = (await api("/api/sessions?archived=only")).total;
  if (archived) throw new Error(`${name} left ${archived} session(s) archived; the demo data must end unchanged`);
  const out = path.join(outDir, `${name}.gif`);
  const r = spawnSync(process.env.PYTHON || "python", [path.join(here, "make_gif.py"), path.join(framesRoot, name), out,
    "--ms", String(FRAME_MS)], { encoding: "utf8" });
  process.stdout.write(r.stdout || r.stderr);
}

fs.mkdirSync(outDir, { recursive: true });
const firstResult = `(document.querySelectorAll("#results .item")[0])`;
const secondResult = `(document.querySelectorAll("#results .item")[1])`;

// Hero: search across projects, open a match, see which sessions contain it.
await record("demo", async () => {
  await go("/");
  await hold(900);
  await click("#q", 200);
  await type("retry backoff");
  await hold(1200);
  await click(secondResult, 1400);
  await click(`.seg [data-view=sessions]`, 1300);
  await click(`(document.querySelectorAll("#sessions .item")[0])`, 2200);
});

// Search: type, results from several projects, step through them.
await record("search", async () => {
  await go("/");
  await hold(700);
  await click("#q", 200);
  await type("retry backoff");
  await hold(1400);
  await moveTo(`(document.querySelector("#detail .msg.hit"))`);
  await hold(1200);
  await click(secondResult, 1800);
});

// Sessions containing matches, ranked with counts.
await record("sessions-matches", async () => {
  await go(`/?q=${encodeURIComponent("migration OR retry OR idempotency")}`, 1800);
  await hold(900);
  await click(`.seg [data-view=sessions]`, 1300);
  await moveTo(`(document.querySelector("#sessions .pill.hits"))`);
  await hold(1000);
  await click(`(document.querySelectorAll("#sessions .item")[1])`, 2200);
});

// History: open a session, the continue options, details.
await record("history", async () => {
  await go("/");
  await hold(800);
  await click(`(document.querySelectorAll("#sessions .item")[1])`, 1400);
  await click(`[data-act=more]`, 1500);
  await moveTo(`[data-act=bring]`);
  await hold(900);
  await click(`[data-act=chat]`, 2200);
});

// Context pack: add two messages, describe the task, build, send to chat.
await record("context-pack", async () => {
  await go("/", 600);
  await evaluate(`localStorage.removeItem("cch.pack")`);
  await go(`/?q=${encodeURIComponent("retry backoff")}`, 1800);
  await hold(700);
  await click(`(document.querySelector("#detail .msg.hit [data-a=pack]"))`, 1000);
  await click(secondResult, 1200);
  await click(`(document.querySelector("#detail .msg.hit [data-a=pack]"))`, 1000);
  await click(`[data-tab=pack]`, 900);
  await click("#pTask", 200);
  await type("Add retries to the invoice export job", 45);
  await click("#pBuild", 1600);
  await click("#pChat", 2200);
  await evaluate(`localStorage.removeItem("cch.pack")`);
});

// Archive: select sessions, archive them, find them under Archived, restore.
await record("archive", async () => {
  await go("/");
  await hold(700);
  await click(`(document.querySelectorAll("#sessions .pick")[1])`, 500);
  await click(`(document.querySelectorAll("#sessions .pick")[2])`, 700);
  await click("#bulkArchive", 1500);
  await moveTo("#sarch");
  await evaluate(`window.__cursor.ripple(${mouse.x}, ${mouse.y}); (() => { const s = document.querySelector("#sarch"); s.value = "only"; s.dispatchEvent(new Event("change")); })()`);
  await hold(1500);
  await click(`(document.querySelectorAll("#sessions .pick")[0])`, 400);
  await click(`(document.querySelectorAll("#sessions .pick")[1])`, 600);
  await click("#bulkUnarchive", 1500);
  await evaluate(`(() => { const s = document.querySelector("#sarch"); s.value = "exclude"; s.dispatchEvent(new Event("change")); })()`);
  await hold(1200);
});

ws.close();
proc.kill();
await sleep(500);
fs.rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
