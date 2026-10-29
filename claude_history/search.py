"""Global search and context retrieval. No repository scoping happens here:
every query runs over the whole index unless the caller asks for a filter.

The `Retriever` protocol keeps ranking pluggable so a semantic/vector retriever
can be fused with FTS later (see docs/roadmap.md) without touching callers.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

HL_START, HL_END = "\x02", "\x03"
ROLES = ("user", "assistant", "thinking", "tool_use", "tool_result", "summary", "memory")
# Down-weight tool chatter relative to the human/assistant conversation.
_ROLE_WEIGHT_SQL = ("CASE m.role WHEN 'tool_result' THEN 0.55 WHEN 'tool_use' THEN 0.75 "
                    "WHEN 'thinking' THEN 0.8 ELSE 1.0 END")
_OPERATORS = {"AND", "OR", "NOT"}
_TOKEN_RE = re.compile(r'-?"[^"]*"\*?|\(|\)|[^\s()]+')


@dataclass
class SearchFilters:
    project: str | None = None          # project key, name or cwd substring
    session: str | None = None          # session id (prefix allowed)
    roles: list[str] = field(default_factory=list)
    date_from: str | None = None        # ISO date / datetime, inclusive
    date_to: str | None = None          # ISO date / datetime, inclusive
    file: str | None = None             # substring of a referenced file path
    include_sidechain: bool = True
    include_memory: bool = True
    include_archived: bool = False


class Retriever(Protocol):
    def search(self, conn: sqlite3.Connection, query: str, filters: SearchFilters,
               sort: str, limit: int, offset: int) -> tuple[int, list[dict]]: ...


def build_fts_query(user_query: str) -> str:
    """Translate friendly syntax into a safe FTS5 MATCH expression.

    - "exact phrase"   -> phrase match
    - word             -> quoted term (so punctuation like foo.bar() or a:b is safe;
                          the tokenizer turns it into an adjacent-token phrase)
    - word*            -> prefix match
    - -word / -"phrase"-> NOT
    - AND / OR / NOT / ( ) pass through
    """
    out: list[str] = []
    negs: list[str] = []
    tokens = _TOKEN_RE.findall(user_query)
    if tokens.count("(") != tokens.count(")"):
        tokens = [t for t in tokens if t not in ("(", ")")]

    def emit_operand(expr: str) -> None:
        # FTS5 only allows implicit AND between plain phrases, so make it explicit.
        if out and (out[-1] == ")" or out[-1].startswith('"')):
            out.append("AND")
        out.append(expr)

    for tok in tokens:
        if tok in _OPERATORS:
            if out and out[-1] not in _OPERATORS and out[-1] != "(":
                out.append(tok)
            continue
        if tok == "(":
            emit_operand("(")
            continue
        if tok == ")":
            while out and out[-1] in _OPERATORS:
                out.pop()
            if out and out[-1] == "(":
                out.pop()  # empty group, e.g. the "()" of foo()
                if out and out[-1] == "AND":
                    out.pop()
            else:
                out.append(")")
            continue
        neg = tok.startswith("-") and len(tok) > 1
        if neg:
            tok = tok[1:]
        prefix = tok.endswith("*") and len(tok) > 1
        if prefix:
            tok = tok[:-1]
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            tok = tok[1:-1]
        tok = tok.replace('"', '""').strip()
        if not tok or not re.search(r"\w", tok):
            continue
        term = f'"{tok}"' + ("*" if prefix else "")
        if neg:
            negs.append(term)
        else:
            emit_operand(term)
    while out and out[-1] in _OPERATORS:
        out.pop()
    if out and negs:
        # FTS5 NOT is binary, so exclusions apply to the whole positive expression.
        expr = " ".join(out)
        return (f"({expr})" if len(out) > 1 else expr) + "".join(f" NOT {t}" for t in negs)
    return " ".join(out)


def _filter_sql(f: SearchFilters) -> tuple[list[str], list[Any]]:
    where, args = [], []
    if f.project:
        where.append("(p.key = ? OR p.name = ? COLLATE NOCASE OR p.cwd LIKE ?)")
        args += [f.project, f.project, f"%{f.project}%"]
    if f.session:
        where.append("m.session_id LIKE ?")
        args.append(f.session + "%")
    roles = [r for r in f.roles if r in ROLES]
    if roles:
        where.append(f"m.role IN ({','.join('?' * len(roles))})")
        args += roles
    if f.date_from:
        where.append("m.ts >= ?")
        args.append(f.date_from)
    if f.date_to:
        where.append("m.ts <= ?")
        args.append(f.date_to + ("T23:59:59.999Z" if len(f.date_to) == 10 else ""))
    if f.file:
        # Paths are stored JSON-encoded, so backslashes appear doubled.
        pat = "%" + json.dumps(f.file)[1:-1] + "%"
        where.append("(m.files LIKE ? OR s.files LIKE ?)")
        args += [pat, pat]
    if not f.include_sidechain:
        where.append("m.is_sidechain = 0")
    if not f.include_archived:
        where.append("s.archived_at IS NULL")
    if not f.include_memory:
        where.append("m.role != 'memory'")
    return where, args


_SELECT = ("SELECT m.id, m.session_id, m.ts, m.role, m.tool_name, m.is_sidechain, m.is_error, m.files,"
           " s.title, s.cwd AS session_cwd, s.git_branch, p.name AS project, p.key AS project_key,"
           " p.cwd AS project_cwd")
_FROM = (" FROM messages m JOIN sessions s ON s.id = m.session_id"
         " LEFT JOIN projects p ON p.id = s.project_id")


def _order(sort: str, rank_expr: str | None) -> str:
    if sort == "newest":
        return "m.ts DESC, m.id DESC"
    if sort == "oldest":
        return "m.ts ASC, m.id ASC"
    return rank_expr or "m.ts DESC"


class FtsRetriever:
    def search(self, conn, query, filters, sort, limit, offset):
        match = build_fts_query(query)
        if not match:
            return 0, []
        where, args = _filter_sql(filters)
        cond = " AND ".join(["messages_fts MATCH ?"] + where)
        base = f" FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid" \
               f" JOIN sessions s ON s.id = m.session_id LEFT JOIN projects p ON p.id = s.project_id" \
               f" WHERE {cond}"
        total = conn.execute("SELECT COUNT(*)" + base, [match, *args]).fetchone()[0]
        rank = f"bm25(messages_fts) * {_ROLE_WEIGHT_SQL}"
        sql = (f"{_SELECT}, snippet(messages_fts, 0, '{HL_START}', '{HL_END}', '…', 40) AS snippet,"
               f" {rank} AS score{base} ORDER BY {_order(sort, 'score')} LIMIT ? OFFSET ?")
        rows = conn.execute(sql, [match, *args, limit, offset]).fetchall()
        return total, [_row(r) for r in rows]


class SubstringRetriever:
    """Literal substring search: for fragments the tokenizer splits oddly."""

    def search(self, conn, query, filters, sort, limit, offset):
        if not query.strip():
            return 0, []
        where, args = _filter_sql(filters)
        cond = " AND ".join(["instr(lower(m.text), lower(?)) > 0"] + where)
        total = conn.execute(f"SELECT COUNT(*){_FROM} WHERE {cond}", [query, *args]).fetchone()[0]
        rows = conn.execute(
            f"{_SELECT}, m.text AS fulltext, 0 AS score{_FROM} WHERE {cond}"
            f" ORDER BY {_order(sort, None)} LIMIT ? OFFSET ?", [query, *args, limit, offset]).fetchall()
        out = []
        for r in rows:
            d = _row(r)
            d["snippet"] = _literal_snippet(r["fulltext"], query)
            out.append(d)
        return total, out


def _literal_snippet(text: str, needle: str, width: int = 160) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return text[: width * 2]
    a, b = max(0, i - width), min(len(text), i + len(needle) + width)
    return (("…" if a else "") + text[a:i] + HL_START + text[i:i + len(needle)] + HL_END
            + text[i + len(needle):b] + ("…" if b < len(text) else ""))


def _row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys() if k != "fulltext"}
    d["files"] = json.loads(d["files"]) if d.get("files") else []
    d["cwd"] = d.pop("session_cwd") or d.get("project_cwd")
    if d.get("score") is not None:
        d["score"] = round(-float(d["score"]), 4)
    return d


RETRIEVERS: dict[str, Retriever] = {"fts": FtsRetriever(), "substring": SubstringRetriever()}


def search(conn: sqlite3.Connection, query: str, filters: SearchFilters | None = None,
           sort: str = "relevance", page: int = 1, page_size: int = 25, mode: str = "fts") -> dict:
    filters = filters or SearchFilters()
    page, page_size = max(1, page), max(1, min(page_size, 200))
    retriever = RETRIEVERS.get(mode, RETRIEVERS["fts"])
    try:
        total, results = retriever.search(conn, query, filters, sort, page_size, (page - 1) * page_size)
    except sqlite3.OperationalError as exc:
        # Malformed FTS expression: fall back to literal matching rather than failing.
        if mode != "fts":
            raise
        total, results = RETRIEVERS["substring"].search(
            conn, query, filters, sort, page_size, (page - 1) * page_size)
        mode = f"substring (fts error: {exc})"
    return {"query": query, "mode": mode, "sort": sort, "page": page, "page_size": page_size,
            "total": total, "results": results}


# ---------------------------------------------------------------- sessions ---

def session_meta(conn: sqlite3.Connection, session_id: str) -> dict | None:
    r = conn.execute(
        "SELECT s.*, p.name AS project, p.key AS project_key, p.cwd AS project_cwd, p.repo_url"
        " FROM sessions s LEFT JOIN projects p ON p.id = s.project_id WHERE s.id = ?",
        (session_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["files"] = json.loads(d["files"]) if d.get("files") else []
    d["cwd"] = d.get("cwd") or d.get("project_cwd")
    d["cwd_exists"] = bool(d["cwd"]) and Path(d["cwd"]).exists()
    d["sources"] = [dict(x) for x in conn.execute(
        "SELECT path, kind, status, error, bad_lines, last_indexed FROM sources WHERE session_id = ?",
        (session_id,))]
    # Resuming needs Claude Code's own transcript and the original directory.
    has_transcript = any(s["kind"] == "main" and s["status"] != "missing" for s in d["sources"])
    if session_id.startswith("memory:"):
        d["resume_blocker"] = "memory files are not sessions"
    elif not has_transcript:
        d["resume_blocker"] = "Claude Code deleted this transcript; continue via claude-history or a context pack"
    elif not d["cwd_exists"]:
        d["resume_blocker"] = "the original directory no longer exists"
    else:
        d["resume_blocker"] = None
    d["resumable"] = d["resume_blocker"] is None
    d["resume_command"] = resume_command(session_id) if d["resumable"] else None
    return d


def resume_command(session_id: str) -> str:
    # `cch resume` cds to the original directory and finds the claude binary itself.
    return f"cch resume {session_id}"


def _messages(rows) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["files"] = json.loads(d["files"]) if d.get("files") else []
        out.append(d)
    return out


_MSG_COLS = "id, session_id, ts, role, tool_name, is_sidechain, is_error, text, files"


def get_session_messages(conn, session_id: str, offset: int = 0, limit: int = 200,
                         roles: list[str] | None = None, include_sidechain: bool = False) -> dict:
    where, args = ["session_id = ?"], [session_id]
    if roles:
        where.append(f"role IN ({','.join('?' * len(roles))})")
        args += roles
    if not include_sidechain:
        where.append("is_sidechain = 0")
    cond = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {cond}", args).fetchone()[0]
    rows = conn.execute(f"SELECT {_MSG_COLS} FROM messages WHERE {cond} ORDER BY ts, id LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    return {"session": session_meta(conn, session_id), "total": total, "offset": offset,
            "messages": _messages(rows)}


def get_context(conn, message_id: int, before: int = 3, after: int = 3,
                include_tools: bool = True) -> dict | None:
    hit = conn.execute(f"SELECT {_MSG_COLS} FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not hit:
        return None
    roles_cond = "" if include_tools else " AND role IN ('user','assistant','summary','memory')"
    side_cond = "" if hit["is_sidechain"] else " AND is_sidechain = 0"
    base = f"session_id = ?{side_cond}{roles_cond}"
    prev = conn.execute(
        f"SELECT {_MSG_COLS} FROM messages WHERE {base} AND (ts < ? OR (ts = ? AND id < ?))"
        " ORDER BY ts DESC, id DESC LIMIT ?",
        (hit["session_id"], hit["ts"], hit["ts"], hit["id"], before)).fetchall()
    nxt = conn.execute(
        f"SELECT {_MSG_COLS} FROM messages WHERE {base} AND (ts > ? OR (ts = ? AND id > ?))"
        " ORDER BY ts, id LIMIT ?",
        (hit["session_id"], hit["ts"], hit["ts"], hit["id"], after)).fetchall()
    msgs = _messages(list(reversed(prev)) + [hit] + list(nxt))
    for m in msgs:
        m["is_hit"] = m["id"] == message_id
    return {"session": session_meta(conn, hit["session_id"]), "hit_id": message_id, "messages": msgs}


def list_sessions(conn, q: str | None = None, project: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, file: str | None = None, sort: str = "newest",
                  page: int = 1, page_size: int = 50, include_memory: bool = False,
                  archived: str = "exclude") -> dict:
    """archived: "exclude" (default), "only" or "all"."""
    where, args = [], []
    if not include_memory:
        where.append("s.id NOT LIKE 'memory:%'")
    if archived == "only":
        where.append("s.archived_at IS NOT NULL")
    elif archived != "all":
        where.append("s.archived_at IS NULL")
    if q:
        where.append("(s.title LIKE ? OR s.first_prompt LIKE ? OR s.id LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"{q}%"]
    if project:
        where.append("(p.key = ? OR p.name = ? COLLATE NOCASE OR p.cwd LIKE ?)")
        args += [project, project, f"%{project}%"]
    if date_from:
        where.append("s.last_activity >= ?")
        args.append(date_from)
    if date_to:
        where.append("s.started <= ?")
        args.append(date_to + ("T23:59:59.999Z" if len(date_to) == 10 else ""))
    if file:
        where.append("s.files LIKE ?")
        args.append("%" + json.dumps(file)[1:-1] + "%")
    cond = ("WHERE " + " AND ".join(where)) if where else ""
    order = {"oldest": "s.started ASC", "messages": "s.message_count DESC",
             "title": "s.title COLLATE NOCASE"}.get(sort, "s.last_activity DESC")
    base = f" FROM sessions s LEFT JOIN projects p ON p.id = s.project_id {cond}"
    total = conn.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
    page, page_size = max(1, page), max(1, min(page_size, 500))
    rows = conn.execute(
        "SELECT s.id, s.title, s.cwd, s.git_branch, s.started, s.last_activity, s.message_count,"
        " s.user_count, s.files, s.summary, s.source_missing, s.entrypoint, s.archived_at,"
        " p.name AS project, p.key AS project_key"
        f"{base} ORDER BY {order} LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size]).fetchall()
    sessions = []
    for r in rows:
        d = dict(r)
        d["files"] = (json.loads(d["files"]) if d.get("files") else [])[:8]
        sessions.append(d)
    return {"total": total, "page": page, "page_size": page_size, "sessions": sessions}


def sessions_matching(conn, query: str, filters: SearchFilters | None = None, sort: str = "relevance",
                      page: int = 1, page_size: int = 50) -> dict:
    """Sessions that contain messages matching `query`, with a hit count each.

    Same shape as list_sessions, so the UI can show either in one list.
    """
    filters = filters or SearchFilters(include_memory=False)
    match = build_fts_query(query)
    page, page_size = max(1, page), max(1, min(page_size, 500))
    if not match:
        return {"total": 0, "page": page, "page_size": page_size, "sessions": []}
    where, args = _filter_sql(filters)
    cond = " AND ".join(["messages_fts MATCH ?"] + where)
    # bm25() can't be used inside an aggregate, so rank each hit first (MATERIALIZED
    # stops SQLite from flattening the CTE back into the GROUP BY).
    cte = (f"WITH h AS MATERIALIZED (SELECT m.session_id, bm25(messages_fts) AS score"
           f" FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
           f" JOIN sessions s ON s.id = m.session_id LEFT JOIN projects p ON p.id = s.project_id WHERE {cond}),"
           f" g AS (SELECT session_id, COUNT(*) AS hits, MIN(score) AS best FROM h GROUP BY session_id)")
    order = {"newest": "s.last_activity DESC", "oldest": "s.started ASC", "messages": "s.message_count DESC",
             "title": "s.title COLLATE NOCASE"}.get(sort, "g.best ASC")
    total = conn.execute(f"{cte} SELECT COUNT(*) FROM g", [match, *args]).fetchone()[0]
    rows = conn.execute(
        f"{cte} SELECT s.id, s.title, s.cwd, s.git_branch, s.started, s.last_activity, s.message_count,"
        " s.user_count, s.files, s.summary, s.source_missing, s.entrypoint, s.archived_at,"
        " p.name AS project, p.key AS project_key, g.hits"
        " FROM g JOIN sessions s ON s.id = g.session_id LEFT JOIN projects p ON p.id = s.project_id"
        f" ORDER BY {order} LIMIT ? OFFSET ?", [match, *args, page_size, (page - 1) * page_size]).fetchall()
    sessions = []
    for r in rows:
        d = dict(r)
        d["files"] = (json.loads(d["files"]) if d.get("files") else [])[:8]
        sessions.append(d)
    return {"total": total, "page": page, "page_size": page_size, "sessions": sessions, "match": query}


def list_projects(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT p.key, p.name, p.cwd, p.repo_url, p.first_seen, p.last_seen,"
        " COUNT(s.id) FILTER (WHERE s.id NOT LIKE 'memory:%' AND s.archived_at IS NULL) AS sessions,"
        " COUNT(s.id) FILTER (WHERE s.archived_at IS NOT NULL) AS archived,"
        " COALESCE(SUM(s.message_count) FILTER (WHERE s.archived_at IS NULL), 0) AS messages"
        " FROM projects p LEFT JOIN sessions s ON s.project_id = p.id"
        " GROUP BY p.id ORDER BY p.last_seen DESC NULLS LAST")]
