"""SQLite schema, connection setup and versioned migrations."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 3

# Each entry migrates from version (index) to (index + 1).
MIGRATIONS: list[str] = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        key         TEXT NOT NULL UNIQUE,      -- encoded dir name under projects/
        name        TEXT NOT NULL,
        cwd         TEXT,
        repo_url    TEXT,
        first_seen  TEXT,
        last_seen   TEXT
    );

    -- One row per physical file we read. Original files are never written.
    CREATE TABLE sources (
        id            INTEGER PRIMARY KEY,
        path          TEXT NOT NULL UNIQUE,
        project_id    INTEGER REFERENCES projects(id),
        session_id    TEXT,
        kind          TEXT NOT NULL,           -- main | subagent | memory
        size          INTEGER NOT NULL DEFAULT 0,
        mtime         REAL NOT NULL DEFAULT 0,
        indexed_bytes INTEGER NOT NULL DEFAULT 0,
        line_count    INTEGER NOT NULL DEFAULT 0,
        head_hash     TEXT,                    -- fingerprint of the first 4 KiB
        status        TEXT NOT NULL DEFAULT 'pending', -- ok | error | missing | pending
        error         TEXT,
        bad_lines     INTEGER NOT NULL DEFAULT 0,
        parser_version INTEGER NOT NULL DEFAULT 0,
        last_indexed  TEXT
    );

    CREATE TABLE sessions (
        id            TEXT PRIMARY KEY,        -- Claude Code sessionId
        project_id    INTEGER REFERENCES projects(id),
        title         TEXT,
        first_prompt  TEXT,
        cwd           TEXT,
        git_branch    TEXT,
        entrypoint    TEXT,
        version       TEXT,
        started       TEXT,
        last_activity TEXT,
        message_count INTEGER NOT NULL DEFAULT 0,
        user_count    INTEGER NOT NULL DEFAULT 0,
        files         TEXT,                    -- JSON list, most-touched first
        summary       TEXT,
        source_missing INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE messages (
        id          INTEGER PRIMARY KEY,
        session_id  TEXT NOT NULL REFERENCES sessions(id),
        source_id   INTEGER NOT NULL REFERENCES sources(id),
        uuid        TEXT NOT NULL,             -- record uuid (or synthetic for memory)
        part        INTEGER NOT NULL,          -- content-block index within the record
        line_no     INTEGER NOT NULL,          -- 0-based line in the source file
        ts          TEXT,
        role        TEXT NOT NULL,             -- user|assistant|tool_use|tool_result|summary|memory
        tool_name   TEXT,
        is_sidechain INTEGER NOT NULL DEFAULT 0,
        is_error    INTEGER NOT NULL DEFAULT 0,
        text        TEXT NOT NULL,
        files       TEXT,                      -- JSON list of referenced paths
        UNIQUE (session_id, uuid, part)
    );
    CREATE INDEX messages_session_ts ON messages(session_id, ts, id);
    CREATE INDEX messages_source ON messages(source_id);
    CREATE INDEX sessions_project ON sessions(project_id, last_activity);

    CREATE VIRTUAL TABLE messages_fts USING fts5(
        text, content='messages', content_rowid='id',
        tokenize = "unicode61 remove_diacritics 2"
    );
    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;

    -- Reserved for the optional semantic layer (see docs/roadmap).
    CREATE TABLE embeddings (
        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
        model      TEXT NOT NULL,
        vector     BLOB NOT NULL
    );
    """,
    # v2: "HEAD" means "not a git repo / detached", not a branch name.
    """
    UPDATE sessions SET git_branch = NULL WHERE git_branch = 'HEAD';
    """,
    # v3: archiving (hide, reversible) and deletion tombstones (kept out of syncs).
    """
    ALTER TABLE sessions ADD COLUMN archived_at TEXT;
    CREATE INDEX sessions_archived ON sessions(archived_at);
    CREATE TABLE deleted_sessions (
        id            TEXT PRIMARY KEY,
        project_key   TEXT,
        project_name  TEXT,
        title         TEXT,
        cwd           TEXT,
        message_count INTEGER,
        started       TEXT,
        last_activity TEXT,
        deleted_at    TEXT NOT NULL
    );
    """,
]


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.isolation_level = None  # explicit transactions only (BEGIN IMMEDIATE ... COMMIT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        migrate(conn)
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Index schema v{version} is newer than this app (v{SCHEMA_VERSION}); upgrade the app."
        )
    while version < SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have migrated.
            version = current_version(conn)
            if version >= SCHEMA_VERSION:
                conn.execute("COMMIT")
                break
            for stmt in _split(MIGRATIONS[version]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version + 1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        version += 1


def _split(script: str) -> list[str]:
    """Split a migration script into statements, keeping trigger bodies intact."""
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        joined = "\n".join(buf).strip()
        if joined.endswith(";") and sqlite3.complete_statement(joined):
            out.append(joined)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
