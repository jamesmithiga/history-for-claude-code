"use strict";
// Run: npm test   (needs `npm run bundle-python` first for the backend test)
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const net = require("net");
const { samePath, findClaudeBinary, buildWebviewHtml, Backend } = require("../lib/core");

test("samePath ignores case, slashes and trailing separators on Windows only", () => {
  assert.ok(samePath("C:\\Work\\My Repo", "c:/work/my repo/", "win32"));
  assert.ok(!samePath("C:\\Work\\My Repo", "C:\\Work\\My Repo2", "win32"));
  assert.ok(!samePath("/home/a/Repo", "/home/a/repo", "linux"));
  assert.ok(!samePath(null, "/x"));
});

test("findClaudeBinary prefers setting, then the Claude Code extension's bundled binary", () => {
  const exists = p => p === "C:\\custom\\claude.exe" || p.endsWith(path.join("native-binary", "claude.exe"));
  assert.strictEqual(findClaudeBinary({ configured: "C:\\custom\\claude.exe", claudeExtensionPath: "X", platform: "win32", exists }),
    "C:\\custom\\claude.exe");
  assert.strictEqual(findClaudeBinary({ configured: "", claudeExtensionPath: "C:\\ext\\anthropic.claude-code-2.1.312", platform: "win32", exists }),
    path.join("C:\\ext\\anthropic.claude-code-2.1.312", "resources", "native-binary", "claude.exe"));
  assert.strictEqual(findClaudeBinary({ configured: "", claudeExtensionPath: undefined, platform: "win32", exists }), null);
});

test("buildWebviewHtml injects CSP, nonce'd bridge and webview URIs", () => {
  const index = fs.readFileSync(path.join(__dirname, "..", "..", "claude_history", "web", "index.html"), "utf8");
  const html = buildWebviewHtml(index, { cssUri: "vscode-resource:/app.css", jsUri: "vscode-resource:/app.js",
    cspSource: "vscode-resource:", nonce: "abc123", initialQuery: 'say "hi"</script>' });
  assert.match(html, /Content-Security-Policy" content="default-src 'none'; style-src vscode-resource: 'unsafe-inline'; script-src 'nonce-abc123'/);
  assert.match(html, /<script nonce="abc123" src="vscode-resource:\/app.js"><\/script>/);
  assert.match(html, /window\.cchHost/);
  assert.ok(!html.includes('href="app.css"') && !html.includes('src="app.js"'));
  // A "</script>" inside the initial query must not close the bridge script early.
  assert.strictEqual(html.split("</script>").length - 1, 2);
  assert.ok(html.includes('"say \\"hi\\"\\u003c/script>"'));
  assert.throws(() => buildWebviewHtml("<html></html>", { cssUri: "a", jsUri: "b", cspSource: "c", nonce: "n" }));
});

function freePort() {
  return new Promise(resolve => {
    const s = net.createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}

test("Backend starts the bundled Python server, serves requests, and restarts after it dies", { timeout: 120000 }, async () => {
  const cch = path.join(__dirname, "..", "python", "cch.py");
  assert.ok(fs.existsSync(cch), "run npm run bundle-python first");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cch-ext-"));
  const claudeDir = path.join(tmp, "claude");
  const proj = path.join(claudeDir, "projects", "C--repo-x");
  fs.mkdirSync(proj, { recursive: true });
  const sid = "eeeeeeee-0000-0000-0000-000000000005";
  fs.writeFileSync(path.join(proj, `${sid}.jsonl`), JSON.stringify({
    type: "user", sessionId: sid, uuid: "u1", timestamp: "2026-01-01T00:00:00Z", cwd: tmp,
    message: { role: "user", content: "configure the flux capacitor" } }) + "\n");
  const port = await freePort();
  const backend = new Backend({
    python: process.env.PYTHON || (process.platform === "win32" ? "python" : "python3"),
    cchPath: cch, port,
    env: Object.assign({}, process.env, { CLAUDE_HISTORY_DATA: path.join(tmp, "data"), CLAUDE_CONFIG_DIR: claudeDir }),
  });
  try {
    await backend.ensure();
    await backend.request("POST", "/api/sync", { full: false });
    let res;
    for (let i = 0; i < 40; i++) {
      res = await backend.request("GET", "/api/search?q=capacitor");
      if (res.total) break;
      await new Promise(r => setTimeout(r, 250));
    }
    assert.strictEqual(res.total, 1);
    const ses = await backend.request("GET", `/api/session/${sid}?limit=1`);
    assert.strictEqual(ses.session.resumable, true); // transcript present, cwd exists
    await assert.rejects(backend.request("GET", "/api/session/nope"), /not found/);
    // Kill the server; the next request transparently restarts it.
    backend.child.kill();
    await new Promise(r => setTimeout(r, 500));
    const st = await backend.request("GET", "/api/status");
    assert.strictEqual(st.sessions, 1);
  } finally {
    backend.dispose();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
