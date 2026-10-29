"use strict";
// Claude History for VS Code: global search over every Claude Code session on
// this machine, context packs, and one-click resume in the integrated terminal.
// The index/search engine is the bundled Python app (python/cch.py).

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFile } = require("child_process");
const { samePath, findClaudeBinary, buildWebviewHtml, makeNonce, Backend } = require("./lib/core");

const PENDING_KEY = "claudeHistory.pendingResume";
const PENDING_MAX_AGE_MS = 3 * 60 * 1000;

let backend;
let panel;
let output;
let statusItem;
let lastEditor;

function cfg() {
  return vscode.workspace.getConfiguration("claudeHistory");
}

function log(msg) {
  output.appendLine(`[${new Date().toISOString().slice(11, 19)}] ${msg}`);
}

function webDir(context) {
  return path.join(context.extensionPath, "python", "claude_history", "web");
}

// ------------------------------------------------------------------ panel --
async function openPanel(context, query) {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Active);
    if (query) panel.webview.postMessage({ type: "search", q: query });
    return;
  }
  panel = vscode.window.createWebviewPanel("claudeHistory", "Claude History", vscode.ViewColumn.Active, {
    enableScripts: true,
    retainContextWhenHidden: true,
    localResourceRoots: [vscode.Uri.file(webDir(context))],
  });
  panel.iconPath = new vscode.ThemeIcon("history");
  panel.onDidDispose(() => { panel = undefined; }, null, context.subscriptions);
  panel.webview.onDidReceiveMessage(msg => handleMessage(context, msg), null, context.subscriptions);
  panel.webview.html = `<!doctype html><body style="font-family:var(--vscode-font-family);padding:20px">Starting Claude History…</body>`;
  try {
    await backend.ensure();
  } catch (e) {
    panel.webview.html = `<!doctype html><body style="font-family:var(--vscode-font-family);padding:20px">
      <h3>Claude History backend could not start</h3><p>${escapeHtml(e.message)}</p>
      <p>See the "Claude History" output channel for details.</p></body>`;
    log(`Backend start failed: ${e.message}`);
    return;
  }
  const w = panel.webview;
  const index = fs.readFileSync(path.join(webDir(context), "index.html"), "utf8");
  w.html = buildWebviewHtml(index, {
    cssUri: w.asWebviewUri(vscode.Uri.file(path.join(webDir(context), "app.css"))).toString(),
    jsUri: w.asWebviewUri(vscode.Uri.file(path.join(webDir(context), "app.js"))).toString(),
    cspSource: w.cspSource,
    nonce: makeNonce(),
    initialQuery: query || null,
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function handleMessage(context, msg) {
  const reply = (ok, dataOrError) => panel && panel.webview.postMessage(
    ok ? { reply: true, id: msg.id, ok: true, data: dataOrError }
       : { reply: true, id: msg.id, ok: false, error: String(dataOrError && dataOrError.message || dataOrError) });
  try {
    switch (msg.type) {
      case "api": {
        if (typeof msg.path !== "string" || !msg.path.startsWith("/api/")) throw new Error("bad path");
        const data = await backend.request(msg.body === undefined ? "GET" : "POST", msg.path, msg.body);
        return reply(true, data);
      }
      case "copy":
        await vscode.env.clipboard.writeText(String(msg.text));
        return reply(true, null);
      case "open":
        await openPath(String(msg.path));
        return reply(true, null);
      case "resume":
        return reply(true, await resumeSession(context, String(msg.sessionId), String(msg.mode)));
      case "chat":
        await openClaudeChat(undefined, String(msg.text));
        return reply(true, null);
      case "insert":
        await insertText(String(msg.text));
        return reply(true, null);
      case "saveAs":
        await saveAs(String(msg.text));
        return reply(true, null);
      default:
        throw new Error(`unknown message ${msg.type}`);
    }
  } catch (e) {
    return reply(false, e);
  }
}

async function openPath(p) {
  let stat;
  try { stat = fs.statSync(p); } catch { throw new Error(`${p} no longer exists`); }
  const uri = vscode.Uri.file(p);
  if (stat.isDirectory()) {
    await vscode.commands.executeCommand("vscode.openFolder", uri, { forceNewWindow: true });
  } else {
    await vscode.window.showTextDocument(uri, { preview: true, viewColumn: vscode.ViewColumn.Beside });
  }
}

async function insertText(text) {
  const editor = lastEditor && !lastEditor.document.isClosed ? lastEditor : vscode.window.visibleTextEditors[0];
  if (editor) {
    await editor.edit(b => editor.selections.forEach(sel => b.replace(sel, text)));
    await vscode.window.showTextDocument(editor.document, editor.viewColumn);
  } else {
    const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: text });
    await vscode.window.showTextDocument(doc);
  }
}

async function saveAs(text) {
  const base = vscode.workspace.workspaceFolders?.[0]?.uri;
  const name = `context-pack-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "")}.md`;
  const target = await vscode.window.showSaveDialog({
    defaultUri: base ? vscode.Uri.joinPath(base, name) : undefined, filters: { Markdown: ["md"] },
  });
  if (target) {
    await vscode.workspace.fs.writeFile(target, Buffer.from(text, "utf8"));
    vscode.window.showInformationMessage(`Saved ${target.fsPath}`);
  }
}

// -------------------------------------------------------------------- mcp --
// The claude-history MCP server lets Claude itself search this index from any
// repo ("Bring into this chat" relies on it). It is registered at user scope in
// ~/.claude.json by the bundled CLI (`cch install-mcp`), which backs the file up.
function claudeJsonPath() {
  const dir = process.env.CLAUDE_CONFIG_DIR;
  return dir ? path.join(dir, ".claude.json") : path.join(os.homedir(), ".claude.json");
}

function mcpEntry() {
  try {
    return JSON.parse(fs.readFileSync(claudeJsonPath(), "utf8")).mcpServers?.["claude-history"] || null;
  } catch { return null; }
}

function runCch(context, args) {
  return new Promise((resolve, reject) => {
    execFile(backend.python, [path.join(context.extensionPath, "python", "cch.py"), ...args],
      { windowsHide: true, timeout: 60000, env: Object.assign({}, process.env, { PYTHONIOENCODING: "utf-8" }) },
      (err, stdout, stderr) => err ? reject(new Error((stderr || err.message).trim().split("\n").pop())) : resolve(stdout));
  });
}

async function installMcp(context, { quiet = false } = {}) {
  await runCch(context, ["install-mcp"]);
  log("Registered the claude-history MCP server");
  if (!quiet) {
    vscode.window.showInformationMessage(
      "Registered the claude-history MCP server for all Claude Code projects (previous ~/.claude.json backed up). " +
      "New Claude sessions can now search your history.");
  }
}

/** Re-point a registration made by an older version of THIS extension (its install path changes per version). */
async function repairMcpPath(context) {
  const entry = mcpEntry();
  const script = entry?.args?.[0];
  if (!script || !/history-for-claude-code-/.test(script)) return; // not ours (e.g. a manual install): leave it
  const current = path.join(context.extensionPath, "python", "cch.py");
  if (samePath(script, current) || fs.existsSync(script)) return;
  await installMcp(context, { quiet: true });
}

async function ensureMcpForBring(context) {
  if (mcpEntry()) return true;
  const pick = await vscode.window.showWarningMessage(
    "\"Bring into this chat\" needs the claude-history MCP server so Claude can read your history. Register it now?",
    { modal: true, detail: "Adds a 'claude-history' entry to ~/.claude.json (a backup is made first)." },
    "Register");
  if (pick !== "Register") return false;
  await installMcp(context);
  return true;
}

// ----------------------------------------------------------------- resume --
function claudeExecutable() {
  const claudeExt = vscode.extensions.getExtension("anthropic.claude-code");
  const found = findClaudeBinary({ configured: cfg().get("claudePath"), claudeExtensionPath: claudeExt?.extensionPath });
  return found || "claude"; // last resort: whatever is on PATH
}

function runResumeTerminal(session) {
  const term = vscode.window.createTerminal({
    name: `Claude: ${(session.title || session.id).slice(0, 40)}`,
    cwd: session.cwd,
    shellPath: claudeExecutable(),
    shellArgs: ["--resume", session.id],
    iconPath: new vscode.ThemeIcon("history"),
  });
  term.show();
  log(`Resumed ${session.id} in ${session.cwd}`);
}

// Claude Code's own extension opens a session in its chat panel through the
// `claude-vscode.editor.open(sessionId, initialPrompt)` command (the same one its
// vscode://anthropic.claude-code/open?session=&prompt= link handler uses). It is
// not in that extension's README, so every call falls back to the terminal.
const CLAUDE_OPEN_CMD = "claude-vscode.editor.open";

async function claudeChatAvailable() {
  return (await vscode.commands.getCommands(true)).includes(CLAUDE_OPEN_CMD);
}

async function openClaudeChat(sessionId, prompt) {
  if (!(await claudeChatAvailable())) {
    throw new Error("The Claude Code extension isn't installed or active in this window");
  }
  await vscode.commands.executeCommand(CLAUDE_OPEN_CMD, sessionId, prompt);
}

function bringPrompt(s) {
  const when = (s.last_activity || "").slice(0, 10);
  return `Continue the work from my earlier Claude Code session "${s.title || s.id}" ` +
    `(session ${s.id}, project ${s.project || "?"}, originally in ${s.cwd || "an unknown folder"}, last active ${when}). ` +
    `Use the claude-history tools (get_session with session_id "${s.id}") to load it, summarise where we left off ` +
    `and what was decided, then carry on here. It is history from another folder: check it against this ` +
    `workspace before applying anything.`;
}

const isOpenHere = cwd => (vscode.workspace.workspaceFolders || []).some(f => samePath(f.uri.fsPath, cwd));

/**
 * mode:
 *  chat  - reopen the conversation in Claude's chat panel. The panel only finds
 *          sessions of the folder it runs in, so for another folder this opens
 *          that folder in a new window and resumes there.
 *  bring - new chat in THIS workspace, pre-filled to load the session via claude-history.
 *  term  - `claude --resume` in an integrated terminal in this window (original folder).
 * Returns what actually happened so the UI can say so.
 */
async function resumeSession(context, sessionId, mode) {
  const res = await backend.request("GET", `/api/session/${encodeURIComponent(sessionId)}?limit=1`);
  const s = res.session;
  if (mode === "bring") {
    if (!(await ensureMcpForBring(context))) throw new Error("cancelled");
    await openClaudeChat(undefined, bringPrompt(s));
    return "bring";
  }
  if (!s.resumable) {
    throw new Error(`${s.resume_blocker}. Use "Bring into this chat" instead: it loads the session through claude-history.`);
  }
  if (mode === "term" || mode === "here") {
    runResumeTerminal(s);
    return "term";
  }
  if (isOpenHere(s.cwd)) {
    try {
      await openClaudeChat(s.id);
      log(`Opened ${s.id} in Claude chat`);
      return "chat";
    } catch (e) {
      log(`Chat open failed (${e.message}); using terminal`);
      runResumeTerminal(s);
      return "term";
    }
  }
  // Open the original folder in a new window; that window's instance of this
  // extension picks up the pending resume on activation.
  await context.globalState.update(PENDING_KEY, { id: s.id, title: s.title, cwd: s.cwd, at: Date.now() });
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(s.cwd), { forceNewWindow: true });
  return "window";
}

async function consumePendingResume(context) {
  const p = context.globalState.get(PENDING_KEY);
  if (!p) return;
  if (Date.now() - p.at > PENDING_MAX_AGE_MS) {
    await context.globalState.update(PENDING_KEY, undefined);
    return;
  }
  if (!isOpenHere(p.cwd)) return; // meant for another window
  await context.globalState.update(PENDING_KEY, undefined);
  // Claude Code activates on startup too; give its commands a moment to register.
  for (let i = 0; i < 40 && !(await claudeChatAvailable()); i++) await new Promise(r => setTimeout(r, 250));
  try {
    await openClaudeChat(p.id);
    log(`Opened ${p.id} in Claude chat (new window)`);
  } catch (e) {
    log(`Chat open failed (${e.message}); using terminal`);
    runResumeTerminal(p);
  }
}

async function pickAndResume(context) {
  await backend.ensure();
  const qp = vscode.window.createQuickPick();
  qp.placeholder = "Pick a Claude Code session from any project to continue";
  qp.matchOnDescription = true;
  qp.matchOnDetail = true;
  qp.busy = true;
  qp.show();
  try {
    const res = await backend.request("GET", "/api/sessions?page_size=300&sort=newest");
    qp.items = res.sessions.map(s => ({
      label: s.title || s.id,
      description: `${s.project || ""}${s.source_missing ? " · transcript deleted" : ""}`,
      detail: `${(s.last_activity || "").slice(0, 16).replace("T", " ")} · ${s.message_count} msgs · ${s.cwd || ""}`,
      session: s,
    }));
  } catch (e) {
    qp.hide();
    vscode.window.showErrorMessage(`Claude History: ${e.message}`);
    return;
  }
  qp.busy = false;
  const choice = await new Promise(resolve => {
    qp.onDidAccept(() => { resolve(qp.selectedItems[0]); qp.hide(); });
    qp.onDidHide(() => resolve(undefined));
  });
  qp.dispose();
  if (!choice) return;
  const here = isOpenHere(choice.session.cwd);
  const mode = (await vscode.window.showQuickPick([
    { label: "$(comment-discussion) Continue in chat", mode: "chat",
      description: here ? "reopen this conversation in Claude's chat panel" : `opens ${choice.session.cwd} in a new window` },
    { label: "$(arrow-right) Bring into this chat", mode: "bring",
      description: "new chat in this workspace that loads the session via claude-history" },
    { label: "$(terminal) Continue in terminal", mode: "term", description: "claude --resume in this window" },
  ], { placeHolder: choice.label }))?.mode;
  if (!mode) return;
  try {
    await resumeSession(context, choice.session.id, mode);
  } catch (e) {
    vscode.window.showWarningMessage(`Can't resume: ${e.message}`);
  }
}

// ----------------------------------------------------------------- status --
async function refreshStatus() {
  try {
    const s = await backend.raw("GET", "/api/status", undefined, 3000);
    statusItem.text = "$(history) History";
    const ago = s.last_sync ? Math.round((Date.now() - Date.parse(s.last_sync)) / 60000) : null;
    statusItem.tooltip = `Claude History: ${s.sessions} sessions · ${s.messages.toLocaleString()} messages across ` +
      `${s.projects} projects\nLast sync: ${ago === null ? "never" : ago < 1 ? "just now" : ago + " min ago"}\nClick to search`;
    statusItem.backgroundColor = s.sync_error || (s.sources_by_status && s.sources_by_status.error)
      ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  } catch {
    statusItem.text = "$(history) History";
    statusItem.tooltip = "Claude History: backend not running (click to start)";
  }
}

// --------------------------------------------------------------- activate --
function activate(context) {
  output = vscode.window.createOutputChannel("Claude History");
  const c = cfg();
  backend = new Backend({
    python: c.get("pythonPath") || (process.platform === "win32" ? "python" : "python3"),
    cchPath: path.join(context.extensionPath, "python", "cch.py"),
    port: c.get("port") || 8765,
    log,
  });
  context.subscriptions.push(output, { dispose: () => backend.dispose() });

  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.command = "claudeHistory.open";
  statusItem.text = "$(history) History";
  statusItem.show();
  context.subscriptions.push(statusItem);

  lastEditor = vscode.window.activeTextEditor;
  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(e => { if (e) lastEditor = e; }));

  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, async (...a) => {
    try { await fn(...a); } catch (e) { vscode.window.showErrorMessage(`Claude History: ${e.message}`); log(e.stack || e.message); }
  }));
  reg("claudeHistory.open", () => openPanel(context));
  reg("claudeHistory.searchSelection", async () => {
    const ed = vscode.window.activeTextEditor;
    let q = "";
    if (ed) {
      q = ed.document.getText(ed.selection).trim();
      if (!q) {
        const r = ed.document.getWordRangeAtPosition(ed.selection.active);
        if (r) q = ed.document.getText(r);
      }
    }
    if (!q) q = await vscode.window.showInputBox({ prompt: "Search all Claude Code history" }) || "";
    q = q.replace(/\s+/g, " ").slice(0, 300);
    if (q.length > 80 || /[^\w\s.\-]/.test(q)) q = `"${q.replace(/"/g, " ")}"`; // treat code/error text as a phrase
    if (q) await openPanel(context, q);
  });
  reg("claudeHistory.resumeSession", () => pickAndResume(context));
  reg("claudeHistory.sync", async () => {
    await backend.ensure();
    await backend.request("POST", "/api/sync", { full: false });
    vscode.window.setStatusBarMessage("$(sync~spin) Claude History sync started", 3000);
    setTimeout(refreshStatus, 2500);
  });
  reg("claudeHistory.showLog", () => output.show());
  reg("claudeHistory.installMcp", () => installMcp(context));

  consumePendingResume(context).catch(e => log(`Pending resume failed: ${e.message}`));
  repairMcpPath(context).catch(e => log(`MCP path repair failed: ${e.message}`));
  if (c.get("autoStart")) {
    backend.ensure().then(refreshStatus, e => log(`Backend start failed: ${e.message}`));
  }
  const timer = setInterval(refreshStatus, 60000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  // Exposed as the extension's exports for the integration tests.
  return { _test: { resumeSession, consumePendingResume, bringPrompt, PENDING_KEY } };
}

function deactivate() {
  if (backend) backend.dispose();
}

module.exports = { activate, deactivate };
