"""Model Context Protocol server (stdio, stdlib only).

Registered at *user* scope in Claude Code, it gives every session — whatever
repository is open — tools to search and read the global history index.
Responses are compact Markdown with message/session ids for follow-up calls.
Nothing is written to stdout except protocol messages.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone

from . import __version__, contextpack, db, ingest, search
from .config import Config

log = logging.getLogger("claude_history.mcp")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Global Claude Code history across ALL repositories on this machine. Use it when the user refers to "
    "earlier work ('like we did before', 'the fix from the other project'), or when a past decision, "
    "error, or implementation elsewhere would help. Start with search_history, then get_message_context "
    "for surrounding conversation. Results describe past work in other directories, not the current repo: "
    "say which project/session/date a fact comes from, and verify against current code."
)

_ROLE_ENUM = list(search.ROLES)
TOOLS = [
    {
        "name": "search_history",
        "description": "Full-text search over every indexed Claude Code session from every project. Supports "
                       "\"exact phrases\", identifiers (load_workbook, foo.bar), prefix*, -exclude, OR. Returns "
                       "ranked snippets with message_id/session_id for get_message_context.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "project": {"type": "string", "description": "Project name, key, or directory substring"},
            "roles": {"type": "array", "items": {"type": "string", "enum": _ROLE_ENUM}},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            "file": {"type": "string", "description": "Substring of a file path touched in the session"},
            "sort": {"type": "string", "enum": ["relevance", "newest", "oldest"]},
            "mode": {"type": "string", "enum": ["fts", "substring"],
                     "description": "fts (default, tokenised) or substring (literal text)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "page": {"type": "integer", "minimum": 1, "default": 1},
        }, "required": ["query"]},
    },
    {
        "name": "get_message_context",
        "description": "Show a message with its surrounding conversation plus session metadata "
                       "(project, original directory, dates, resume command).",
        "inputSchema": {"type": "object", "properties": {
            "message_id": {"type": "integer"},
            "before": {"type": "integer", "default": 4, "minimum": 0, "maximum": 40},
            "after": {"type": "integer", "default": 4, "minimum": 0, "maximum": 40},
            "include_tools": {"type": "boolean", "default": True},
            "max_chars_per_message": {"type": "integer", "default": 2500},
        }, "required": ["message_id"]},
    },
    {
        "name": "get_session",
        "description": "Read a whole historical session in order (paginated).",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 40, "maximum": 200},
            "include_tools": {"type": "boolean", "default": False},
            "max_chars_per_message": {"type": "integer", "default": 1500},
        }, "required": ["session_id"]},
    },
    {
        "name": "list_sessions",
        "description": "List sessions across all projects, newest first; filter by title/prompt text, project, dates, or file.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "project": {"type": "string"},
            "date_from": {"type": "string"}, "date_to": {"type": "string"}, "file": {"type": "string"},
            "limit": {"type": "integer", "default": 20, "maximum": 100},
        }},
    },
    {
        "name": "list_projects",
        "description": "List every project/directory that has Claude Code history, with session counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "build_context_pack",
        "description": "Assemble selected messages (with neighbours) into an attributed, secret-redacted, "
                       "token-budgeted Markdown context document.",
        "inputSchema": {"type": "object", "properties": {
            "message_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "before": {"type": "integer", "default": 2}, "after": {"type": "integer", "default": 2},
            "task": {"type": "string"}, "max_tokens": {"type": "integer", "default": 4000},
            "include_tools": {"type": "boolean", "default": True},
        }, "required": ["message_ids"]},
    },
    {
        "name": "sync_history",
        "description": "Index new/changed Claude Code transcripts now (normally automatic). full=true re-reads everything.",
        "inputSchema": {"type": "object", "properties": {"full": {"type": "boolean", "default": False}}},
    },
]


def _clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f" …[+{len(text) - n} chars]"


def _day(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


class HistoryMCP:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        self._last_sync: float | None = None  # None: never synced in this process

    def _maybe_sync(self, force: bool = False, full: bool = False) -> ingest.SyncStats | None:
        if force or self._last_sync is None or time.monotonic() - self._last_sync > self.cfg.mcp_sync_max_age:
            try:
                stats = ingest.sync(self.cfg, self.conn, full=full)
            except Exception:
                log.exception("sync failed")
                stats = None
            self._last_sync = time.monotonic()
            return stats
        return None

    # ---------------------------------------------------------------- tools --
    def search_history(self, a: dict) -> str:
        self._maybe_sync()
        limit = max(1, min(int(a.get("limit") or 10), 50))
        f = search.SearchFilters(project=a.get("project"), roles=a.get("roles") or [],
                                 date_from=a.get("date_from"), date_to=a.get("date_to"), file=a.get("file"))
        res = search.search(self.conn, a["query"], f, sort=a.get("sort") or "relevance",
                            page=int(a.get("page") or 1), page_size=limit, mode=a.get("mode") or "fts")
        if not res["total"]:
            return (f"No matches for {a['query']!r} across all projects. Try fewer/other terms, a prefix "
                    "(term*), or mode='substring'.")
        lines = [f"{res['total']} matches for {a['query']!r} (page {res['page']}, {res['mode']}). "
                 "Historical sessions from any project; not the current repo."]
        for r in res["results"]:
            snip = r["snippet"].replace(search.HL_START, "**").replace(search.HL_END, "**")
            snip = " ".join(snip.split())
            lines.append(
                f"\n- message_id={r['id']} | {r['role']}{'/' + r['tool_name'] if r.get('tool_name') else ''}"
                f" | {_day(r['ts'])} | project: {r['project']} | dir: {r['cwd']}\n"
                f"  session: {r['title'] or ''} ({r['session_id']})\n  > {_clip(snip, 400)}")
        if res["total"] > res["page"] * limit:
            lines.append(f"\n(more: page={res['page'] + 1})")
        return "\n".join(lines)

    def _session_head(self, s: dict) -> str:
        parts = [f"Session: {s.get('title') or s['id']}",
                 f"id: {s['id']} | project: {s.get('project')} | dir: {s.get('cwd')}"
                 + ("" if s.get("cwd_exists") else " (directory no longer exists)"),
                 f"active: {_day(s.get('started'))} → {_day(s.get('last_activity'))} UTC | {s.get('message_count')} messages"
                 + (f" | branch: {s['git_branch']}" if s.get("git_branch") else "")]
        if s.get("source_missing"):
            parts.append("note: original transcript file has been deleted; content preserved in the index")
        if s.get("resume_command"):
            parts.append(f"resume (user runs in a terminal): {s['resume_command']}")
        elif s.get("resume_blocker"):
            parts.append(f"cannot be resumed: {s['resume_blocker']}")
        if s.get("files"):
            parts.append("files: " + ", ".join(s["files"][:12]))
        return "\n".join(parts)

    def _render_msgs(self, msgs: list[dict], n: int) -> str:
        out = []
        for m in msgs:
            tag = m["role"] + (f"/{m['tool_name']}" if m.get("tool_name") else "")
            mark = "  ◀ MATCH" if m.get("is_hit") else ""
            out.append(f"\n[{m['id']}] {tag} · {_day(m['ts'])}{mark}\n{_clip(m['text'], n)}")
        return "\n".join(out)

    def get_message_context(self, a: dict) -> str:
        ctx = search.get_context(self.conn, int(a["message_id"]), int(a.get("before", 4)),
                                 int(a.get("after", 4)), bool(a.get("include_tools", True)))
        if not ctx:
            return f"No message with id {a['message_id']}."
        return self._session_head(ctx["session"]) + "\n" + self._render_msgs(
            ctx["messages"], int(a.get("max_chars_per_message") or 2500))

    def get_session(self, a: dict) -> str:
        roles = None if a.get("include_tools") else ["user", "assistant", "summary", "memory"]
        res = search.get_session_messages(self.conn, a["session_id"], int(a.get("offset") or 0),
                                          min(int(a.get("limit") or 40), 200), roles)
        if not res["session"]:
            return f"No session {a['session_id']!r}. Use list_sessions or search_history to find ids."
        shown = res["offset"] + len(res["messages"])
        more = f"\n\n(showing {res['offset']}–{shown} of {res['total']}; next offset={shown})" if shown < res["total"] else ""
        return self._session_head(res["session"]) + "\n" + self._render_msgs(
            res["messages"], int(a.get("max_chars_per_message") or 1500)) + more

    def list_sessions(self, a: dict) -> str:
        self._maybe_sync()
        res = search.list_sessions(self.conn, q=a.get("query"), project=a.get("project"),
                                   date_from=a.get("date_from"), date_to=a.get("date_to"), file=a.get("file"),
                                   page_size=min(int(a.get("limit") or 20), 100))
        lines = [f"{res['total']} sessions (showing {len(res['sessions'])}):"]
        for s in res["sessions"]:
            lines.append(f"- {s['id']} | {_day(s['last_activity'])} | {s['project']} | {s['message_count']} msgs"
                         f" | {s['title']}" + (" | source deleted" if s["source_missing"] else ""))
        return "\n".join(lines)

    def list_projects(self, a: dict) -> str:
        rows = search.list_projects(self.conn)
        return "\n".join([f"{len(rows)} projects:"] + [
            f"- {p['name']} | {p['cwd'] or p['key']} | {p['sessions']} sessions | last {_day(p['last_seen'])}"
            for p in rows])

    def build_context_pack(self, a: dict) -> str:
        items = [contextpack.PackItem(int(i), int(a.get("before", 2)), int(a.get("after", 2)))
                 for i in a["message_ids"]]
        pack = contextpack.build_pack(self.conn, items, task=a.get("task"),
                                      max_tokens=int(a.get("max_tokens") or 4000),
                                      include_tools=bool(a.get("include_tools", True)),
                                      do_redact=self.cfg.redact_packs)
        return pack["markdown"]

    def sync_history(self, a: dict) -> str:
        stats = self._maybe_sync(force=True, full=bool(a.get("full")))
        if not stats:
            return "Sync failed; see the log at " + str(self.cfg.log_path)
        return json.dumps(stats.as_dict())

    # ------------------------------------------------------------- protocol --
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "claude-history", "version": __version__},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                name, args = params.get("name"), params.get("arguments") or {}
                fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
                if not fn:
                    return _err(mid, -32602, f"unknown tool {name}")
                try:
                    text, is_error = fn(args), False
                except Exception as exc:
                    log.exception("tool %s failed", name)
                    text, is_error = f"{type(exc).__name__}: {exc}", True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return _err(mid, -32601, f"method not found: {method}")
        except Exception as exc:
            log.exception("request failed")
            return _err(mid, -32603, str(exc))
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def run(cfg: Config) -> None:
    server = HistoryMCP(cfg)

    def startup_sync() -> None:
        # Every Claude Code session start (any repo) refreshes the index, so
        # transcripts are captured before Claude Code's own cleanup removes them.
        try:
            ingest.sync(cfg)
            server._last_sync = time.monotonic()
        except Exception:
            log.exception("startup sync failed")

    threading.Thread(target=startup_sync, daemon=True, name="startup-sync").start()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    log.info("MCP server started %s", datetime.now(timezone.utc).isoformat())
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            resp = _err(None, -32700, "parse error")
        else:
            if isinstance(msg, list):
                resp = [r for r in (server.handle(m) for m in msg) if r] or None
            else:
                resp = server.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
