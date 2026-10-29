"""Archive and delete sessions from the index.

Neither operation touches Claude Code's own transcript files.

- Archive hides a session from search, session lists, project counts and the
  MCP tools. It is reversible, and archived sessions stay readable by id.
- Delete removes a session's messages from the index and records a tombstone in
  `deleted_sessions`, so syncing never re-indexes it. Restoring removes the
  tombstone and re-reads the original transcript, which only works while Claude
  Code still has that file.
"""
from __future__ import annotations

import sqlite3

from . import db


def _clean_ids(ids) -> list[str]:
    out = []
    for i in ids or []:
        i = str(i).strip()
        if i and i not in out:
            out.append(i)
    if not out:
        raise ValueError("no session ids given")
    if len(out) > 1000:
        raise ValueError("too many session ids in one request (max 1000)")
    return out


def set_archived(conn: sqlite3.Connection, ids, archived: bool = True) -> int:
    ids = _clean_ids(ids)
    stamp = db.now_iso() if archived else None
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for sid in ids:
            n += conn.execute(
                "UPDATE sessions SET archived_at = ? WHERE id = ? AND "
                + ("archived_at IS NULL" if archived else "archived_at IS NOT NULL"),
                (stamp, sid)).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def is_deleted(conn: sqlite3.Connection, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM deleted_sessions WHERE id = ?", (session_id,)).fetchone() is not None


def delete_sessions(conn: sqlite3.Connection, ids) -> int:
    """Remove sessions from the index and keep them out of future syncs."""
    ids = _clean_ids(ids)
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for sid in ids:
            s = conn.execute(
                "SELECT s.id, s.title, s.cwd, s.message_count, s.started, s.last_activity,"
                " p.key AS project_key, p.name AS project_name"
                " FROM sessions s LEFT JOIN projects p ON p.id = s.project_id WHERE s.id = ?", (sid,)).fetchone()
            if not s:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO deleted_sessions(id, project_key, project_name, title, cwd, message_count,"
                " started, last_activity, deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, s["project_key"], s["project_name"], s["title"], s["cwd"], s["message_count"],
                 s["started"], s["last_activity"], db.now_iso()))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            # Keep the source rows so the inventory still accounts for the files.
            conn.execute("UPDATE sources SET status = 'excluded', indexed_bytes = 0, line_count = 0,"
                         " head_hash = NULL, bad_lines = 0 WHERE session_id = ?", (sid,))
            n += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def list_deleted(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM deleted_sessions ORDER BY deleted_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["restorable"] = conn.execute(
            "SELECT 1 FROM sources WHERE session_id = ? AND kind = 'main' AND status != 'missing'",
            (r["id"],)).fetchone() is not None
        out.append(d)
    return out


def restore_sessions(conn: sqlite3.Connection, ids) -> dict:
    """Drop tombstones; the next sync re-reads transcripts that still exist.

    Returns {"restored": [...], "unavailable": [...]}, where unavailable ids had
    no transcript left, so restoring them only clears the tombstone.
    """
    ids = _clean_ids(ids)
    restored, unavailable = [], []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sid in ids:
            if not conn.execute("DELETE FROM deleted_sessions WHERE id = ?", (sid,)).rowcount:
                continue
            # parser_version 0 forces a full re-read of each source on the next sync.
            n = conn.execute("UPDATE sources SET status = 'pending', indexed_bytes = 0, line_count = 0,"
                             " head_hash = NULL, parser_version = 0 WHERE session_id = ? AND status != 'missing'",
                             (sid,)).rowcount
            (restored if n else unavailable).append(sid)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"restored": restored, "unavailable": unavailable}
