"""Resume a historical session in Claude Code.

`claude --resume <id>` only finds a session when run from the session's
original directory, and the `claude` binary is often not on PATH when Claude
Code is used through the VS Code extension (it ships its own copy). This module
locates the newest binary, validates that the session can actually be resumed,
and launches it.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import search

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Editors whose extension folders may contain the Claude Code extension.
EXTENSION_DIRS = (".vscode", ".vscode-insiders", ".cursor", ".antigravity", ".windsurf")


class ResumeError(Exception):
    pass


@dataclass
class ResumePlan:
    session_id: str
    title: str | None
    cwd: str
    claude: str
    argv: list[str]

    @property
    def command(self) -> str:
        exe = f'& "{self.claude}"' if os.name == "nt" else f'"{self.claude}"'
        return f'cd "{self.cwd}"; {exe} --resume {self.session_id}'


def _version_key(path: Path) -> tuple:
    m = re.search(r"anthropic\.claude-code-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def find_claude(home: Path | None = None) -> str | None:
    """`claude` on PATH, else the newest binary bundled with an editor extension."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = home or Path.home()
    exe = "claude.exe" if os.name == "nt" else "claude"
    candidates = []
    for d in EXTENSION_DIRS:
        candidates += (home / d / "extensions").glob(f"anthropic.claude-code-*/resources/native-binary/{exe}")
    local = home / ".local" / "bin" / exe  # native installer location
    if local.is_file():
        return str(local)
    if not candidates:
        return None
    return str(max(candidates, key=_version_key))


def find_vscode() -> str | None:
    """Path to the VS Code executable (not the code.cmd shim, to avoid batch-file quoting)."""
    shim = shutil.which("code")
    if shim:
        p = Path(shim).resolve()
        for exe in (p.parent.parent / "Code.exe", p.parent.parent / "code"):
            if exe.is_file():
                return str(exe)
    if os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if base:
                exe = Path(base) / ("Programs" if "Local" in base else "") / "Microsoft VS Code" / "Code.exe"
                if exe.is_file():
                    return str(exe)
    return shim


def resolve_session_id(conn: sqlite3.Connection, ref: str) -> str:
    ref = ref.strip().lower()
    if _UUID_RE.match(ref):
        return ref
    if not re.fullmatch(r"[0-9a-f-]{4,36}", ref):
        raise ResumeError(f"Not a session id or id prefix: {ref!r}")
    rows = conn.execute("SELECT id FROM sessions WHERE id LIKE ? AND id NOT LIKE 'memory:%'",
                        (ref + "%",)).fetchall()
    if len(rows) != 1:
        raise ResumeError(f"{len(rows)} sessions match {ref!r}; give more of the id")
    return rows[0][0]


def plan(conn: sqlite3.Connection, ref: str, claude: str | None = None) -> ResumePlan:
    sid = resolve_session_id(conn, ref)
    meta = search.session_meta(conn, sid)
    if not meta:
        raise ResumeError(f"Session {sid} is not in the index (run `cch sync`).")
    main_sources = [s for s in meta["sources"] if s["kind"] == "main"]
    if not main_sources or all(s["status"] == "missing" for s in main_sources):
        raise ResumeError(
            "Claude Code no longer has this session's transcript, so it can't be resumed. "
            "Its content is still in the index: in any repo, ask Claude to use claude-history "
            f"to load session {sid} and continue, or build a context pack.")
    cwd = meta.get("cwd")
    if not cwd or not Path(cwd).is_dir():
        raise ResumeError(
            f"The session's original directory {cwd!r} no longer exists; `claude --resume` must run there. "
            "Use the claude-history tools or a context pack to continue from another folder.")
    claude = claude or find_claude()
    if not claude:
        raise ResumeError("Could not find the claude executable (not on PATH and no editor extension found).")
    return ResumePlan(sid, meta.get("title"), cwd, claude, [claude, "--resume", sid])


def run_here(p: ResumePlan) -> int:
    """Run in the current terminal (e.g. VS Code's integrated terminal)."""
    return subprocess.call(p.argv, cwd=p.cwd)


def open_folder_in_vscode(cwd: str) -> bool:
    exe = find_vscode()
    if exe:
        subprocess.Popen([exe, cwd], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        return True
    if os.name == "nt":
        os.startfile("vscode://file/" + cwd.replace("\\", "/"))  # protocol handler fallback
        return True
    return False


def launch_in_new_terminal(p: ResumePlan) -> None:
    """Open a separate console window running the resumed session."""
    if os.name == "nt":
        subprocess.Popen(p.argv, cwd=p.cwd, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    elif sys.platform == "darwin":
        script = f'tell app "Terminal" to do script "cd {p.cwd!r} && {p.claude!r} --resume {p.session_id}"'
        subprocess.Popen(["osascript", "-e", script])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
        if not term:
            raise ResumeError("No terminal emulator found; run the command shown instead.")
        subprocess.Popen([term, "--", *p.argv], cwd=p.cwd)
