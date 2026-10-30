"use strict";
// Runs INSIDE a real VS Code instance (see test/integration/run.js). The window's
// workspace is the test session's original folder. Anthropic's Claude Code
// extension isn't loaded in the isolated profile, so a stand-in registers its
// `claude-vscode.editor.open` command to record what we send it.
const vscode = require("vscode");
const assert = require("assert");
const fs = require("fs");

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitFor(fn, ms = 20000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(200);
  }
  throw new Error("timed out waiting");
}

exports.run = async function run() {
  const results = [];
  const step = async (name, fn) => {
    try { await fn(); results.push(`PASS ${name}`); }
    catch (e) { results.push(`FAIL ${name}: ${e.stack || e}`); }
  };
  const ext = vscode.extensions.getExtension("jamesmithiga.history-for-claude-code");
  const sessionId = process.env.CCH_TEST_SESSION;
  const sessionCwd = process.env.CCH_TEST_CWD;
  const chatCalls = [];
  let standIn = vscode.commands.registerCommand("claude-vscode.editor.open", (...args) => { chatCalls.push(args); });
  const ctx = { globalState: { update: async () => {} } };
  let T;

  await step("extension activates", async () => {
    assert.ok(ext, "extension not found");
    await ext.activate();
    assert.ok(ext.isActive);
    T = ext.exports._test;
  });

  await step("commands registered", async () => {
    const all = await vscode.commands.getCommands(true);
    for (const c of ["claudeHistory.open", "claudeHistory.searchSelection", "claudeHistory.resumeSession", "claudeHistory.sync"]) {
      assert.ok(all.includes(c), c);
    }
  });

  await step("search panel opens as a webview tab", async () => {
    await vscode.commands.executeCommand("claudeHistory.open");
    await waitFor(() => vscode.window.tabGroups.all.flatMap(g => g.tabs)
      .some(t => t.label === "Claude History" && t.input instanceof vscode.TabInputWebview));
  });

  await step("Continue in chat: same folder opens the session in Claude's chat panel", async () => {
    chatCalls.length = 0;
    assert.strictEqual(await T.resumeSession(ctx, sessionId, "chat"), "chat");
    assert.deepStrictEqual(chatCalls, [[sessionId, undefined]]);
  });

  await step("Bring into this chat: new chat pre-filled to load the session via claude-history", async () => {
    chatCalls.length = 0;
    assert.strictEqual(await T.resumeSession(ctx, sessionId, "bring"), "bring");
    assert.strictEqual(chatCalls.length, 1);
    const [sid, prompt] = chatCalls[0];
    assert.strictEqual(sid, undefined);
    assert.ok(prompt.includes(sessionId) && prompt.includes("claude-history") && prompt.includes("get_session"), prompt);
  });

  await step("Open folder & continue: the new window opens the pending session in chat", async () => {
    chatCalls.length = 0;
    const store = new Map([[T.PENDING_KEY, { id: sessionId, title: "t", cwd: sessionCwd, at: Date.now() }]]);
    const pctx = { globalState: { get: k => store.get(k), update: async (k, v) => { v === undefined ? store.delete(k) : store.set(k, v); } } };
    await T.consumePendingResume(pctx);
    assert.deepStrictEqual(chatCalls, [[sessionId]].map(([id]) => [id, undefined]));
    assert.ok(!store.has(T.PENDING_KEY), "pending entry should be cleared");
    store.set(T.PENDING_KEY, { id: sessionId, title: "x", cwd: "Z:\\elsewhere", at: Date.now() });
    await T.consumePendingResume(pctx);
    assert.ok(store.has(T.PENDING_KEY), "entry for another folder must be left alone");
  });

  await step("Terminal: claude --resume in an integrated terminal in the original folder", async () => {
    assert.strictEqual(await T.resumeSession(ctx, sessionId, "term"), "term");
    const term = await waitFor(() => vscode.window.terminals.find(t => t.name.startsWith("Claude:")));
    assert.deepStrictEqual(term.creationOptions.shellArgs, ["--resume", sessionId]);
    assert.strictEqual(String(term.creationOptions.cwd).toLowerCase(), sessionCwd.toLowerCase());
    term.dispose();
  });

  await step("without the Claude Code extension, Continue in chat falls back to the terminal", async () => {
    standIn.dispose();
    await sleep(200);
    assert.strictEqual(await T.resumeSession(ctx, sessionId, "chat"), "term");
    const term = await waitFor(() => vscode.window.terminals.find(t => t.name.startsWith("Claude:")));
    term.dispose();
    await assert.rejects(T.resumeSession(ctx, sessionId, "bring"), /isn't installed/);
    standIn = vscode.commands.registerCommand("claude-vscode.editor.open", (...args) => { chatCalls.push(args); });
  });

  await step("Send whole session: the stored conversation goes to a new chat (inline or as a file)", async () => {
    chatCalls.length = 0;
    const mode = await T.resumeSession(ctx, sessionId, "whole");
    assert.ok(mode === "whole" || mode === "whole-file", mode);
    const [sid, prompt] = chatCalls[0];
    assert.strictEqual(sid, undefined);
    if (mode === "whole") assert.ok(prompt.includes("Above is the full conversation") && prompt.includes("# Full transcript"));
    else assert.ok(/is saved at `[^`]+\.md`/.test(prompt), prompt.slice(0, 200));
  });

  await step("Send with tool output: saved to a file, and Claude is told to read it", async () => {
    chatCalls.length = 0;
    assert.strictEqual(await T.resumeSession(ctx, sessionId, "whole-tools"), "whole-file");
    const prompt = chatCalls[0][1];
    const file = /is saved at `([^`]+)`/.exec(prompt)[1];
    assert.ok(fs.existsSync(file), file);
    assert.ok(fs.readFileSync(file, "utf8").includes("# Full transcript"));
  });

  await step("unknown session gives a clear error", async () => {
    await assert.rejects(T.resumeSession(ctx, "00000000-0000-0000-0000-000000000000", "chat"), /not found/);
  });

  standIn.dispose();
  fs.writeFileSync(process.env.CCH_TEST_REPORT, results.join("\n") + "\n");
  if (results.some(r => r.startsWith("FAIL"))) throw new Error(results.join("\n"));
};
