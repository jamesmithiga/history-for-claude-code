"""Discovery and incremental, crash-safe synchronisation of Claude Code history.

Guarantees:
- Original files are opened read-only and never modified.
- Each source file is committed in a single transaction together with its new
  byte offset, so an interrupted sync resumes exactly where it stopped.
- Messages are unique per (session, record uuid, block index): re-syncing never
  duplicates.
- A trailing partial line (session still writing) is deferred to the next sync.
- Sources that disappear are marked 'missing'; their indexed messages are kept.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Config
from .parser import PARSER_VERSION, Limits, iter_complete_lines, parse_line, parse_record

log = logging.getLogger("claude_history.ingest")


@dataclass
class SourceFile:
    path: Path
    project_key: str
    session_id: str
    kind: str  # main | subagent | memory


@dataclass
class SyncStats:
    discovered: int = 0
    indexed: int = 0
    unchanged: int = 0
    errors: int = 0
    missing: int = 0
    new_messages: int = 0
    bad_lines: int = 0
    seconds: float = 0.0
    error_files: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def discover(cfg: Config) -> list[SourceFile]:
    found: list[SourceFile] = []
    for root in cfg.claude_dirs:
        projects = Path(root) / "projects"
        if not projects.is_dir():
            log.warning("No projects directory at %s", projects)
            continue
        for pdir in sorted(p for p in projects.iterdir() if p.is_dir()):
            key = pdir.name
            for f in sorted(pdir.glob("*.jsonl")):
                found.append(SourceFile(f, key, f.stem, "main"))
            for f in sorted(pdir.glob("*/subagents/*.jsonl")):
                found.append(SourceFile(f, key, f.parent.parent.name, "subagent"))
            if cfg.index_memory_files:
                for f in sorted((pdir / "memory").glob("*.md")):
                    found.append(SourceFile(f, key, f"memory:{key}", "memory"))
    return found


HEAD_BYTES = 4096


def _head_hash(path: Path, length: int) -> str:
    """Fingerprint of the first `length` bytes (capped), i.e. of already-indexed
    content only, so appends never look like rewrites."""
    with path.open("rb") as fh:
        return hashlib.sha1(fh.read(min(length, HEAD_BYTES))).hexdigest()


def _read_from(path: Path, offset: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(offset)
        return fh.read()


def _encode(name: str) -> str:
    # Claude Code's directory encoding: every non-alphanumeric character becomes "-".
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def resolve_project_key(key: str) -> str | None:
    """Recover the original directory from an encoded key by walking the disk.

    The encoding is lossy ("-" may have been "\\", " ", "." ...), so match each
    real child directory's encoded name against the remaining key.
    """
    m = re.match(r"^([A-Za-z])--(.*)$", key)
    if m and os.name == "nt":
        cur, rest = Path(f"{m.group(1).upper()}:\\"), m.group(2)
    elif key.startswith("-"):
        cur, rest = Path("/"), key[1:]
    else:
        return None
    while rest:
        try:
            children = sorted((c for c in cur.iterdir() if c.is_dir()), key=lambda c: -len(c.name))
        except OSError:
            return None
        for c in children:
            enc = _encode(c.name)
            if rest == enc or rest.startswith(enc + "-"):
                cur, rest = c, rest[len(enc) + 1:]
                break
        else:
            return None
    return str(cur)


def _decode_project_key(key: str) -> str:
    # Last resort when neither transcripts nor the disk tell us the directory.
    parts = [p for p in key.split("-") if p]
    return " ".join(parts[-2:]) if parts else key


def _path_parts(cwd: str) -> list[str]:
    """Components of a recorded cwd, split on both separators: transcripts can be
    read on a different OS from the one that wrote them."""
    return [p for p in re.split(r"[\\/]+", cwd) if p]


def _dir_name(cwd: str) -> str:
    parts = _path_parts(cwd)
    return parts[-1] if parts else cwd


def _repo_url(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        text = (Path(cwd) / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text, re.S)
    return m.group(1) if m else None


def _ensure_project(conn: sqlite3.Connection, key: str, cwd: str | None) -> int:
    row = conn.execute("SELECT id, cwd FROM projects WHERE key = ?", (key,)).fetchone()
    if row:
        if cwd and not row["cwd"]:
            conn.execute("UPDATE projects SET cwd = ?, name = ?, repo_url = ? WHERE id = ?",
                         (cwd, _dir_name(cwd), _repo_url(cwd), row["id"]))
        return row["id"]
    cwd = cwd or resolve_project_key(key)
    name = _dir_name(cwd) if cwd else _decode_project_key(key)
    cur = conn.execute("INSERT INTO projects(key, name, cwd, repo_url) VALUES (?, ?, ?, ?)",
                       (key, name, cwd, _repo_url(cwd)))
    return cur.lastrowid


def _ensure_session(conn: sqlite3.Connection, sid: str, project_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO sessions(id, project_id) VALUES (?, ?)", (sid, project_id))


def _reset_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM messages WHERE source_id = ?", (source_id,))
    conn.execute("UPDATE sources SET indexed_bytes = 0, line_count = 0, bad_lines = 0 WHERE id = ?",
                 (source_id,))


def _index_jsonl(conn, src: SourceFile, row, data: bytes, base_offset: int, base_line: int,
                 limits: Limits) -> tuple[int, int, int, int]:
    """Insert messages from complete lines. Returns (new_offset, lines, inserted, bad)."""
    offset, line_no, inserted, bad = base_offset, base_line, 0, 0
    meta: dict = {}
    titles: list[tuple[bool, str]] = []
    project_id = row["project_id"]
    _ensure_session(conn, src.session_id, project_id)
    for end, line in iter_complete_lines(data):
        try:
            rec = parse_line(line)
        except ValueError:
            bad += 1
            rec = None
        if rec is not None:
            res = parse_record(rec, limits)
            if res.title:
                titles.append((res.title_is_custom, res.title))
            for k in ("cwd", "git_branch", "entrypoint", "version"):
                v = getattr(res, k)
                if v:
                    meta[k] = v
            for m in res.messages:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages(session_id, source_id, uuid, part, line_no, ts, role,"
                    " tool_name, is_sidechain, is_error, text, files) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (src.session_id, row["id"], m.uuid, m.part, line_no, m.ts, m.role, m.tool_name,
                     int(m.is_sidechain or src.kind == "subagent"), int(m.is_error), m.text,
                     json.dumps(m.files) if m.files else None))
                inserted += cur.rowcount
        offset = base_offset + end
        line_no += 1

    if src.kind == "main":
        if meta.get("cwd"):
            _ensure_project(conn, src.project_key, meta["cwd"])
        sets, vals = [], []
        for k in ("cwd", "git_branch", "entrypoint", "version"):
            if k in meta:
                sets.append(f"{k} = ?")
                vals.append(meta[k])
        custom = [t for c, t in titles if c]
        auto = [t for c, t in titles if not c]
        if custom:
            sets.append("title = ?"); vals.append(custom[-1])
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, '1')",
                         (f"custom_title:{src.session_id}",))
        elif auto and not db.get_meta(conn, f"custom_title:{src.session_id}"):
            sets.append("title = ?"); vals.append(auto[-1])
        if sets:
            conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*vals, src.session_id))
    return offset, line_no, inserted, bad


def _index_memory(conn, src: SourceFile, row, path: Path, mtime: float) -> int:
    _ensure_session(conn, src.session_id, row["project_id"])
    conn.execute("DELETE FROM messages WHERE source_id = ?", (row["id"],))
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return 0
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
    conn.execute(
        "INSERT INTO messages(session_id, source_id, uuid, part, line_no, ts, role, text, files)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (src.session_id, row["id"], f"memory:{path.name}", 0, 0, ts, "memory", text,
         json.dumps([str(path)])))
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?",
                 (f"Memory — {src.project_key}", src.session_id))
    return 1


def refresh_session(conn: sqlite3.Connection, sid: str) -> None:
    """Recompute derived session fields from its stored messages."""
    agg = conn.execute(
        "SELECT COUNT(*) n, SUM(role = 'user') u, MIN(ts) a, MAX(ts) b FROM messages WHERE session_id = ?",
        (sid,)).fetchone()
    files: Counter = Counter()
    for (fj,) in conn.execute("SELECT files FROM messages WHERE session_id = ? AND files IS NOT NULL", (sid,)):
        try:
            files.update(json.loads(fj))
        except ValueError:
            pass
    first = conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND role = 'user' AND is_sidechain = 0"
        " ORDER BY ts, id LIMIT 1", (sid,)).fetchone()
    compact = conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND role = 'summary' ORDER BY ts DESC, id DESC LIMIT 1",
        (sid,)).fetchone()
    last_reply = conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND role = 'assistant' AND is_sidechain = 0"
        " ORDER BY ts DESC, id DESC LIMIT 1", (sid,)).fetchone()
    first_prompt = first["text"][:400] if first else None
    if compact:
        summary = compact["text"][:600]
    elif first_prompt:
        # Extractive, clearly-labelled fallback: what was asked, and how it ended.
        summary = "Asked: " + first_prompt[:280]
        if last_reply:
            summary += "\nLast reply: " + last_reply["text"][:280]
    else:
        summary = None
    conn.execute(
        "UPDATE sessions SET message_count = ?, user_count = ?, started = ?, last_activity = ?, files = ?,"
        " first_prompt = ?, summary = ?, title = COALESCE(title, ?) WHERE id = ?",
        (agg["n"], agg["u"] or 0, agg["a"], agg["b"],
         json.dumps([f for f, _ in files.most_common(40)]) if files else None,
         first_prompt, summary, (first_prompt or "")[:90] or None, sid))
    conn.execute(
        "UPDATE projects SET first_seen = MIN(COALESCE(first_seen, ?), ?), last_seen = MAX(COALESCE(last_seen, ?), ?)"
        " WHERE id = (SELECT project_id FROM sessions WHERE id = ?) AND ? IS NOT NULL",
        (agg["a"], agg["a"], agg["b"], agg["b"], sid, agg["a"]))


def sync(cfg: Config, conn: sqlite3.Connection | None = None, full: bool = False) -> SyncStats:
    """Incremental sync. full=True re-reads every source from byte 0."""
    t0 = time.perf_counter()
    own = conn is None
    conn = conn or db.connect(cfg.db_path)
    stats = SyncStats()
    limits = Limits(cfg.max_text_chars, cfg.max_tool_result_chars, cfg.max_tool_input_chars)
    sources = discover(cfg)
    stats.discovered = len(sources)
    seen_paths = set()
    try:
        for src in sources:
            seen_paths.add(str(src.path))
            try:
                changed, inserted, bad = _sync_one(conn, src, limits, full)
            except (OSError, sqlite3.DatabaseError, ValueError) as exc:
                stats.errors += 1
                stats.error_files.append({"path": str(src.path), "error": f"{type(exc).__name__}: {exc}"})
                log.error("Failed to index %s: %s", src.path, exc)
                _record_error(conn, src, exc)
                continue
            if changed:
                stats.indexed += 1
            else:
                stats.unchanged += 1
            stats.new_messages += inserted
            stats.bad_lines += bad
        stats.missing = _mark_missing(conn, seen_paths)
        _disambiguate_project_names(conn)
        stats.seconds = round(time.perf_counter() - t0, 3)
        conn.execute("BEGIN IMMEDIATE")
        db.set_meta(conn, "last_sync", db.now_iso())
        db.set_meta(conn, "last_sync_stats", json.dumps(stats.as_dict()))
        conn.execute("COMMIT")
        if full:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('optimize')")
    finally:
        if own:
            conn.close()
    return stats


def _sync_one(conn: sqlite3.Connection, src: SourceFile, limits: Limits, full: bool) -> tuple[bool, int, int]:
    st = src.path.stat()
    row = conn.execute("SELECT * FROM sources WHERE path = ?", (str(src.path),)).fetchone()
    if conn.execute("SELECT 1 FROM deleted_sessions WHERE id = ?", (src.session_id,)).fetchone():
        return _exclude_source(conn, src, row, st)
    up_to_date = (row is not None and not full and row["status"] == "ok"
                  and row["size"] == st.st_size and row["mtime"] == st.st_mtime
                  and row["parser_version"] == PARSER_VERSION)
    if up_to_date:
        return False, 0, 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM sources WHERE path = ?", (str(src.path),)).fetchone()
        if row is None:
            pid = _ensure_project(conn, src.project_key, None)
            cur = conn.execute(
                "INSERT INTO sources(path, project_id, session_id, kind) VALUES (?, ?, ?, ?)",
                (str(src.path), pid, src.session_id, src.kind))
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)).fetchone()
        rewritten = (full or row["parser_version"] != PARSER_VERSION
                     or st.st_size < row["indexed_bytes"]
                     or (row["head_hash"] and row["indexed_bytes"]
                         and _head_hash(src.path, row["indexed_bytes"]) != row["head_hash"]))
        if rewritten:
            _reset_source(conn, row["id"])
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (row["id"],)).fetchone()

        new_bad = 0
        if src.kind == "memory":
            inserted = _index_memory(conn, src, row, src.path, st.st_mtime)
            offset, lines = st.st_size, 1
        else:
            data = _read_from(src.path, row["indexed_bytes"])
            offset, lines, inserted, new_bad = _index_jsonl(
                conn, src, row, data, row["indexed_bytes"], row["line_count"], limits)
        bad = row["bad_lines"] + new_bad
        conn.execute(
            "UPDATE sources SET size = ?, mtime = ?, indexed_bytes = ?, line_count = ?, head_hash = ?,"
            " status = 'ok', error = NULL, bad_lines = ?, parser_version = ?, last_indexed = ? WHERE id = ?",
            (st.st_size, st.st_mtime, offset, lines, _head_hash(src.path, offset), bad, PARSER_VERSION,
             db.now_iso(), row["id"]))
        refresh_session(conn, src.session_id)
        conn.execute("UPDATE sessions SET source_missing = 0 WHERE id = ?", (src.session_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True, inserted, new_bad


def _disambiguate_project_names(conn: sqlite3.Connection) -> None:
    """Two folders both named "api" become "shop\\api" and "billing\\api"."""
    rows = conn.execute("SELECT id, name, cwd FROM projects WHERE cwd IS NOT NULL").fetchall()
    by_base: dict[str, list] = {}
    for r in rows:
        by_base.setdefault(_dir_name(r["cwd"]).lower(), []).append(r)
    updates = []
    for group in by_base.values():
        for r in group:
            parts = _path_parts(r["cwd"])
            sep = "\\" if "\\" in r["cwd"] else "/"
            want = sep.join(parts[-2:]) if len(group) > 1 and len(parts) > 1 else _dir_name(r["cwd"])
            if want != r["name"]:
                updates.append((want, r["id"]))
    if updates:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE projects SET name = ? WHERE id = ?", updates)
        conn.execute("COMMIT")


def _exclude_source(conn: sqlite3.Connection, src: SourceFile, row, st) -> tuple[bool, int, int]:
    """The user deleted this session from the index: track the file, never read it."""
    if row is not None and row["status"] == "excluded":
        return False, 0, 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        pid = _ensure_project(conn, src.project_key, None)
        conn.execute(
            "INSERT INTO sources(path, project_id, session_id, kind, size, mtime, status) VALUES (?,?,?,?,?,?, 'excluded')"
            " ON CONFLICT(path) DO UPDATE SET status = 'excluded', size = excluded.size, mtime = excluded.mtime",
            (str(src.path), pid, src.session_id, src.kind, st.st_size, st.st_mtime))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return False, 0, 0


def _record_error(conn: sqlite3.Connection, src: SourceFile, exc: Exception) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        pid = _ensure_project(conn, src.project_key, None)
        conn.execute(
            "INSERT INTO sources(path, project_id, session_id, kind, status, error) VALUES (?,?,?,?, 'error', ?)"
            " ON CONFLICT(path) DO UPDATE SET status = 'error', error = excluded.error",
            (str(src.path), pid, src.session_id, src.kind, f"{type(exc).__name__}: {exc}"))
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


def _mark_missing(conn: sqlite3.Connection, seen: set[str]) -> int:
    rows = conn.execute("SELECT id, path, session_id, kind FROM sources WHERE status != 'missing'").fetchall()
    gone = [r for r in rows if r["path"] not in seen and not os.path.exists(r["path"])]
    if not gone:
        return conn.execute("SELECT COUNT(*) FROM sources WHERE status = 'missing'").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    for r in gone:
        conn.execute("UPDATE sources SET status = 'missing' WHERE id = ?", (r["id"],))
        if r["kind"] == "main":
            conn.execute("UPDATE sessions SET source_missing = 1 WHERE id = ?", (r["session_id"],))
    conn.execute("COMMIT")
    return conn.execute("SELECT COUNT(*) FROM sources WHERE status = 'missing'").fetchone()[0]


def inventory(conn: sqlite3.Connection) -> dict:
    by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM sources GROUP BY status")}
    by_kind = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM sources GROUP BY kind")}
    errors = [dict(r) for r in conn.execute(
        "SELECT path, error FROM sources WHERE status = 'error' ORDER BY path LIMIT 100")]
    bad = conn.execute("SELECT COALESCE(SUM(bad_lines), 0) FROM sources").fetchone()[0]
    last_stats = db.get_meta(conn, "last_sync_stats")
    return {
        "projects": conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
        "sessions": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id NOT LIKE 'memory:%' AND archived_at IS NULL").fetchone()[0],
        "sessions_archived": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE archived_at IS NOT NULL").fetchone()[0],
        "sessions_deleted": conn.execute("SELECT COUNT(*) FROM deleted_sessions").fetchone()[0],
        "sessions_source_missing": conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_missing = 1").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "sources_by_status": by_status,
        "sources_by_kind": by_kind,
        "skipped_bad_lines": bad,
        "error_sources": errors,
        "last_sync": db.get_meta(conn, "last_sync"),
        "last_sync_stats": json.loads(last_stats) if last_stats else None,
        "schema_version": db.current_version(conn),
        "parser_version": PARSER_VERSION,
    }
