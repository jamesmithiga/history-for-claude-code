"use strict";
// Copy the Python app (../claude_history + ../cch.py) into ./python so the
// .vsix is self-contained apart from a Python interpreter.
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "..");
const dest = path.resolve(__dirname, "..", "python");

fs.rmSync(dest, { recursive: true, force: true });
fs.mkdirSync(dest, { recursive: true });
fs.copyFileSync(path.join(root, "cch.py"), path.join(dest, "cch.py"));
fs.cpSync(path.join(root, "claude_history"), path.join(dest, "claude_history"), {
  recursive: true,
  filter: src => !src.includes("__pycache__") && !src.endsWith(".pyc"),
});
fs.mkdirSync(path.resolve(__dirname, "..", "dist"), { recursive: true });
console.log(`Bundled Python app into ${dest}`);
