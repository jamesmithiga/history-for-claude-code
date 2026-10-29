"use strict";
// Pure helpers and the backend client. No `vscode` import here, so this file is
// unit-testable with plain Node (see test/core.test.js).

const fs = require("fs");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

/** Compare filesystem paths the way the OS does (case-insensitive on Windows). */
function samePath(a, b, platform = process.platform) {
  if (!a || !b) return false;
  const norm = p => {
    let s = path.normalize(String(p)).replace(/[\\/]+$/, "");
    if (platform === "win32") s = s.replace(/\//g, "\\").toLowerCase();
    return s;
  };
  return norm(a) === norm(b);
}

/**
 * Locate the `claude` executable: explicit setting, else the binary bundled with
 * Anthropic's Claude Code extension, else null (caller may fall back to PATH).
 */
function findClaudeBinary({ configured, claudeExtensionPath, platform = process.platform, exists = fs.existsSync }) {
  if (configured && exists(configured)) return configured;
  if (claudeExtensionPath) {
    const exe = platform === "win32" ? "claude.exe" : "claude";
    const p = path.join(claudeExtensionPath, "resources", "native-binary", exe);
    if (exists(p)) return p;
  }
  return null;
}

function makeNonce() {
  return require("crypto").randomBytes(16).toString("base64").replace(/[^A-Za-z0-9]/g, "");
}

// Map the app's colour tokens onto the active VS Code theme.
const THEME_CSS = `
:root, :root:not([data-x]) {
  --bg: var(--vscode-editor-background); --panel: var(--vscode-sideBar-background, var(--vscode-editor-background));
  --sunken: var(--vscode-textCodeBlock-background, var(--vscode-editorWidget-background));
  --border: var(--vscode-panel-border, var(--vscode-widget-border, #8884)); --text: var(--vscode-foreground);
  --muted: var(--vscode-descriptionForeground); --accent: var(--vscode-button-background);
  --accent-weak: var(--vscode-list-inactiveSelectionBackground); --mark: var(--vscode-editor-findMatchHighlightBackground);
  --mark-text: inherit; --sans: var(--vscode-font-family); --mono: var(--vscode-editor-font-family, monospace);
}
body { font-size: var(--vscode-font-size, 13px); }
button.primary { color: var(--vscode-button-foreground); }
input, select, button { background: var(--vscode-input-background); color: var(--vscode-input-foreground); }
button.primary { background: var(--vscode-button-background); }
`;

/** Turn the web app's index.html into a webview document with a strict CSP and host bridge. */
function buildWebviewHtml(indexHtml, { cssUri, jsUri, cspSource, nonce, initialQuery }) {
  const csp = [
    "default-src 'none'",
    `style-src ${cspSource} 'unsafe-inline'`,
    `script-src 'nonce-${nonce}'`,
    `img-src ${cspSource} data:`,
    `font-src ${cspSource}`,
  ].join("; ");
  const bridge = `
(function () {
  const vscode = acquireVsCodeApi();
  let seq = 0;
  const pending = new Map();
  function call(type, payload) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      vscode.postMessage(Object.assign({ id, type }, payload));
    });
  }
  window.addEventListener("message", e => {
    const m = e.data || {};
    if (m.reply) {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id);
      m.ok ? p.resolve(m.data) : p.reject(new Error(m.error || "failed"));
    } else if (m.type === "search" && window.cchSearch) {
      window.cchSearch(m.q);
    }
  });
  window.cchHost = {
    initialQuery: ${JSON.stringify(initialQuery == null ? null : String(initialQuery)).replace(/</g, "\\u003c")},
    api: (path, body) => call("api", { path, body }),
    copy: text => call("copy", { text }),
    open: path => call("open", { path }),
    resume: (sessionId, mode) => call("resume", { sessionId, mode }),
    toChat: text => call("chat", { text }),
    insert: text => call("insert", { text }),
    saveAs: text => call("saveAs", { text }),
  };
})();`;
  let html = indexHtml
    .replace('<link rel="stylesheet" href="app.css">',
      `<meta http-equiv="Content-Security-Policy" content="${csp}">\n<link rel="stylesheet" href="${cssUri}">\n<style>${THEME_CSS}</style>`)
    .replace('<script src="app.js"></script>',
      `<script nonce="${nonce}">${bridge}</script>\n<script nonce="${nonce}" src="${jsUri}"></script>`);
  if (!html.includes(`src="${jsUri}"`) || !html.includes("Content-Security-Policy")) {
    throw new Error("index.html layout changed; cannot build webview");
  }
  return html;
}

/** Talks to the Python backend (`cch serve`), starting it when needed. */
class Backend {
  constructor({ python, cchPath, port, env, log }) {
    this.python = python;
    this.cchPath = cchPath;
    this.port = port;
    this.env = env || process.env;
    this.log = log || (() => {});
    this.child = null;
    this.starting = null;
  }

  raw(method, urlPath, body, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
      const headers = { Host: `127.0.0.1:${this.port}` };
      if (data) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = data.length; }
      const req = http.request({ host: "127.0.0.1", port: this.port, path: urlPath, method, headers, timeout: timeoutMs }, res => {
        const chunks = [];
        res.on("data", c => chunks.push(c));
        res.on("end", () => {
          let parsed;
          try { parsed = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { parsed = { error: `HTTP ${res.statusCode}` }; }
          if (res.statusCode >= 400) {
            const err = new Error(parsed.error || `HTTP ${res.statusCode}`);
            err.status = res.statusCode;
            return reject(err);
          }
          resolve(parsed);
        });
      });
      req.on("timeout", () => req.destroy(new Error("request timed out")));
      req.on("error", reject);
      if (data) req.write(data);
      req.end();
    });
  }

  async alive() {
    try { await this.raw("GET", "/api/status", undefined, 3000); return true; } catch { return false; }
  }

  /** Ensure a server answers on the port; reuse one started by another window. */
  ensure() {
    if (this.starting) return this.starting;
    this.starting = (async () => {
      if (await this.alive()) return;
      this.log(`Starting backend: ${this.python} ${this.cchPath} serve --port ${this.port}`);
      let spawnError = null;
      let stderr = "";
      const child = spawn(this.python, [this.cchPath, "serve", "--port", String(this.port)], {
        env: Object.assign({}, this.env, { PYTHONIOENCODING: "utf-8" }),
        windowsHide: true, stdio: ["ignore", "ignore", "pipe"],
      });
      child.on("error", e => { spawnError = e; });
      child.stderr.on("data", d => { stderr = (stderr + d.toString()).slice(-2000); });
      child.on("exit", code => { if (this.child === child) this.child = null; this.log(`Backend exited (${code})`); });
      this.child = child;
      for (let i = 0; i < 60; i++) {
        if (spawnError) throw new Error(`Could not start Python (${this.python}): ${spawnError.message}. ` +
          "Set 'claudeHistory.pythonPath' to your Python 3.11+ executable.");
        if (child.exitCode !== null) {
          // Another window may have won the race for the port.
          if (await this.alive()) return;
          throw new Error(`Backend exited with code ${child.exitCode}. ${stderr.trim().split("\n").slice(-3).join(" ")}`);
        }
        if (await this.alive()) return;
        await new Promise(r => setTimeout(r, 250));
      }
      throw new Error("Backend did not start within 15 seconds");
    })();
    return this.starting.finally(() => { this.starting = null; });
  }

  /** Request with one automatic (re)start if the server isn't reachable. */
  async request(method, urlPath, body) {
    try {
      return await this.raw(method, urlPath, body);
    } catch (e) {
      if (e.status || !/ECONNREFUSED|ECONNRESET|socket hang up/.test(String(e.message || e.code))) throw e;
      await this.ensure();
      return this.raw(method, urlPath, body);
    }
  }

  dispose() {
    if (this.child && this.child.exitCode === null) this.child.kill();
    this.child = null;
  }
}

module.exports = { samePath, findClaudeBinary, buildWebviewHtml, makeNonce, Backend };
