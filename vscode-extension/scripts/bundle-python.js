"use strict";
// Copy the Python app (../claude_history + ../cch.py) into ./python so the
// .vsix is self-contained apart from a Python interpreter.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const dest = path.resolve(__dirname, "..", "python");

// The backend reports this version so newer windows can replace older servers; keep them in step.
const pyVersion = /__version__\s*=\s*"([^"]+)"/.exec(fs.readFileSync(path.join(root, "claude_history", "__init__.py"), "utf8"))[1];
const extVersion = require("../package.json").version;
if (pyVersion !== extVersion) {
  console.error(`Version mismatch: claude_history/__init__.py is ${pyVersion}, package.json is ${extVersion}`);
  process.exit(1);
}

fs.rmSync(dest, { recursive: true, force: true });
fs.mkdirSync(dest, { recursive: true });
fs.copyFileSync(path.join(root, "cch.py"), path.join(dest, "cch.py"));
fs.cpSync(path.join(root, "claude_history"), path.join(dest, "claude_history"), {
  recursive: true,
  filter: src => !src.includes("__pycache__") && !src.endsWith(".pyc"),
});
fs.mkdirSync(path.resolve(__dirname, "..", "dist"), { recursive: true });
console.log(`Bundled Python app into ${dest}`);
