"use strict";
// Launch the installed VS Code in an isolated profile with this extension in
// development mode and run test/integration/index.js inside it.
// Usage: node test/integration/run.js <sessionId> <sessionCwd>   (the session must be resumable)
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const code = process.env.VSCODE_EXE ||
  path.join(process.env.LOCALAPPDATA || "", "Programs", "Microsoft VS Code", "Code.exe");
const [sessionId, sessionCwd] = process.argv.slice(2);
if (!sessionId || !sessionCwd) throw new Error("usage: run.js <sessionId> <sessionCwd>");

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cch-vscode-it-"));
// The window opens the session's own folder (chat resume needs a matching workspace).
const workspace = sessionCwd;
const report = path.join(tmp, "report.txt");
const ext = path.resolve(__dirname, "..", "..");

const r = spawnSync(code, [
  workspace,
  `--extensionDevelopmentPath=${ext}`,
  `--extensionTestsPath=${path.join(__dirname, "index.js")}`,
  `--user-data-dir=${path.join(tmp, "user-data")}`,
  `--extensions-dir=${path.join(tmp, "extensions")}`,
  "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", "--new-window",
], {
  env: Object.assign({}, process.env, { CCH_TEST_SESSION: sessionId, CCH_TEST_CWD: sessionCwd, CCH_TEST_REPORT: report }),
  encoding: "utf8", timeout: 180000,
});
console.log(fs.existsSync(report) ? fs.readFileSync(report, "utf8") : `no report; exit ${r.status}\n${r.stderr || ""}`);
fs.rmSync(tmp, { recursive: true, force: true });
process.exit(r.status === 0 ? 0 : 1);
