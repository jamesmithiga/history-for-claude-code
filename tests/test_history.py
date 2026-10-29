"""Automated tests. Fixture transcripts mirror the real Claude Code record shapes
(see docs/history-format.md); real-data acceptance checks live in
tests/acceptance_real.py.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_history import contextpack, curation, db, ingest, search  # noqa: E402
from claude_history.config import Config  # noqa: E402
from claude_history.mcp_server import HistoryMCP  # noqa: E402

_counter = 0


def rec_user(sid, text, ts, cwd, **extra):
    global _counter
    _counter += 1
    return {"type": "user", "sessionId": sid, "uuid": f"u-{_counter}", "timestamp": ts, "cwd": cwd,
            "gitBranch": "main", "message": {"role": "user", "content": text}, **extra}


def rec_assistant(sid, blocks, ts, cwd):
    global _counter
    _counter += 1
    return {"type": "assistant", "sessionId": sid, "uuid": f"a-{_counter}", "timestamp": ts, "cwd": cwd,
            "message": {"role": "assistant", "content": blocks}}


def rec_tool_result(sid, text, ts, cwd, is_error=False):
    global _counter
    _counter += 1
    return {"type": "user", "sessionId": sid, "uuid": f"t-{_counter}", "timestamp": ts, "cwd": cwd,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": text, "is_error": is_error}]}}


def write_jsonl(path: Path, records, trailing_partial: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        if trailing_partial:
            fh.write(trailing_partial)


def append_jsonl(path: Path, records):
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for r in records:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cch-test-"))
        self.claude = self.tmp / "claude"
        self.cfg = Config(data_dir=self.tmp / "data", claude_dirs=[self.claude])
        self.cfg.data_dir.mkdir()
        self.projects = self.claude / "projects"
        # Repository A: an auth implementation. Repository B: an unrelated data project.
        self.cwd_a, self.cwd_b = r"C:\work\repo-alpha", r"D:\data\repo-beta"
        self.file_a = self.projects / "C--work-repo-alpha" / "aaaaaaaa-0000-0000-0000-000000000001.jsonl"
        self.file_b = self.projects / "D--data-repo-beta" / "bbbbbbbb-0000-0000-0000-000000000002.jsonl"
        sa, sb = self.file_a.stem, self.file_b.stem
        write_jsonl(self.file_a, [
            {"type": "queue-operation", "operation": "enqueue", "sessionId": sa},
            rec_user(sa, "Implement JWT authentication middleware with refresh tokens", "2026-01-05T10:00:00Z", self.cwd_a),
            rec_assistant(sa, [{"type": "thinking", "thinking": "consider token rotation"},
                               {"type": "text", "text": "I'll add `verify_refresh_token` in auth/tokens.py."}],
                          "2026-01-05T10:00:05Z", self.cwd_a),
            rec_assistant(sa, [{"type": "tool_use", "id": "t1", "name": "Edit",
                                "input": {"file_path": r"C:\work\repo-alpha\auth\tokens.py", "old_string": "a",
                                          "new_string": "def verify_refresh_token(tok): ..."}}],
                          "2026-01-05T10:00:06Z", self.cwd_a),
            rec_tool_result(sa, "File updated", "2026-01-05T10:00:07Z", self.cwd_a),
            rec_user(sa, "<system-reminder>ignore me</system-reminder>Also store api_key=supersecretvalue123 in env",
                     "2026-01-05T10:01:00Z", self.cwd_a),
            {"type": "ai-title", "aiTitle": "JWT auth middleware", "sessionId": sa},
            rec_user(sa, "skill body that should not be indexed", "2026-01-05T10:01:01Z", self.cwd_a, isMeta=True),
        ])
        write_jsonl(self.file_b, [
            rec_user(sb, "Fix the alembic migration error: sqlalchemy.exc.OperationalError duplicate column",
                     "2026-03-10T09:00:00Z", self.cwd_b),
            rec_tool_result(sb, "Traceback ...\nsqlalchemy.exc.OperationalError: duplicate column name: owner_id",
                            "2026-03-10T09:00:03Z", self.cwd_b, is_error=True),
            rec_assistant(sb, [{"type": "text", "text": "Decision: make migration 0042 idempotent by checking the inspector."}],
                          "2026-03-10T09:00:09Z", self.cwd_b),
        ])
        # A subagent transcript and a memory file under repo A.
        sub = self.file_a.parent / sa / "subagents" / "agent-123.jsonl"
        write_jsonl(sub, [rec_assistant(sa, [{"type": "text", "text": "Subagent found ratelimiter config"}],
                                        "2026-01-05T10:00:30Z", self.cwd_a) | {"isSidechain": True}])
        mem = self.file_a.parent / "memory" / "prefs.md"
        mem.parent.mkdir(parents=True)
        mem.write_text("User prefers pytest over unittest for repo-alpha", encoding="utf-8")
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sync(self, **kw):
        return ingest.sync(self.cfg, self.conn, **kw)

    def count(self, sql="SELECT COUNT(*) FROM messages", args=()):
        return self.conn.execute(sql, args).fetchone()[0]


class IngestionTests(Base):
    def test_discovers_all_projects_sessions_subagents_memory(self):
        stats = self.sync()
        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.discovered, 4)
        self.assertEqual(self.count("SELECT COUNT(*) FROM projects"), 2)
        s = search.session_meta(self.conn, self.file_a.stem)
        self.assertEqual(s["title"], "JWT auth middleware")
        self.assertEqual(s["cwd"], self.cwd_a)
        self.assertEqual(s["git_branch"], "main")
        self.assertIn(r"C:\work\repo-alpha\auth\tokens.py", s["files"])
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE is_sidechain = 1"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE role = 'memory'"), 1)

    def test_strips_reminders_and_skips_meta(self):
        self.sync()
        texts = [r[0] for r in self.conn.execute("SELECT text FROM messages WHERE role = 'user'")]
        self.assertFalse(any("system-reminder" in t or "ignore me" in t for t in texts))
        self.assertFalse(any("skill body" in t for t in texts))

    def test_repeated_sync_creates_no_duplicates(self):
        self.sync()
        n_msgs, n_sess = self.count(), self.count("SELECT COUNT(*) FROM sessions")
        for _ in range(3):
            stats = self.sync()
            self.assertEqual(stats.new_messages, 0)
            self.assertEqual(stats.indexed, 0)
        self.sync(full=True)
        self.assertEqual(self.count(), n_msgs)
        self.assertEqual(self.count("SELECT COUNT(*) FROM sessions"), n_sess)

    def test_incremental_append_and_partial_line(self):
        self.sync()
        before = self.count()
        sid = self.file_b.stem
        first_id = self.count("SELECT MIN(id) FROM messages WHERE session_id = ?", (sid,))
        partial = json.dumps(rec_user(sid, "new question about kafka consumer lag", "2026-03-11T00:00:00Z", self.cwd_b))
        append_jsonl(self.file_b, [rec_user(sid, "follow-up about connection pooling", "2026-03-10T10:00:00Z", self.cwd_b)])
        with self.file_b.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(partial[:40])  # session still writing this record
        stats = self.sync()
        self.assertEqual(stats.new_messages, 1)
        self.assertEqual(stats.bad_lines, 0)
        # Only the appended bytes were read: earlier messages kept their row ids.
        self.assertEqual(self.count("SELECT MIN(id) FROM messages WHERE session_id = ?", (sid,)), first_id)
        self.assertEqual(search.search(self.conn, "kafka")["total"], 0)
        with self.file_b.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(partial[40:] + "\n")
        self.sync()
        self.assertEqual(self.count(), before + 2)
        self.assertEqual(search.search(self.conn, "kafka")["total"], 1)

    def test_new_session_becomes_searchable(self):
        self.sync()
        f = self.projects / "C--work-repo-gamma" / "cccccccc-0000-0000-0000-000000000003.jsonl"
        write_jsonl(f, [rec_user(f.stem, "Configure zookeeper quorum", "2026-04-01T00:00:00Z", r"C:\work\repo-gamma")])
        self.sync()
        res = search.search(self.conn, "zookeeper")
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["results"][0]["project"], "repo-gamma")

    def test_malformed_lines_are_skipped_and_counted(self):
        sid = "dddddddd-0000-0000-0000-000000000004"
        f = self.projects / "C--work-broken" / f"{sid}.jsonl"
        write_jsonl(f, ['{"type": "user", "broken', "[1, 2]", "",
                        rec_user(sid, "valid message after garbage", "2026-02-01T00:00:00Z", r"C:\work\broken")])
        stats = self.sync()
        self.assertEqual(stats.errors, 0)
        self.assertEqual(stats.bad_lines, 1)
        self.assertEqual(search.search(self.conn, "garbage")["total"], 1)
        self.assertEqual(ingest.inventory(self.conn)["skipped_bad_lines"], 1)

    def test_deleted_source_keeps_history(self):
        self.sync()
        self.file_b.unlink()
        stats = self.sync()
        self.assertEqual(stats.missing, 1)
        s = search.session_meta(self.conn, self.file_b.stem)
        self.assertEqual(s["source_missing"], 1)
        self.assertFalse(s["cwd_exists"])  # repo-beta directory doesn't exist either
        self.assertGreater(search.search(self.conn, "alembic")["total"], 0)
        self.assertIsNotNone(search.get_context(self.conn, search.search(self.conn, "alembic")["results"][0]["id"]))

    def test_rewritten_file_is_reindexed_without_duplicates(self):
        self.sync()
        sid = self.file_b.stem
        write_jsonl(self.file_b, [rec_user(sid, "completely different content about redis", "2026-05-01T00:00:00Z", self.cwd_b)])
        self.sync()
        self.assertEqual(search.search(self.conn, "alembic", search.SearchFilters(project="repo-beta"))["total"], 0)
        self.assertEqual(search.search(self.conn, "redis")["total"], 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)), 1)

    def test_interrupted_sync_recovers(self):
        real = ingest._index_jsonl
        calls = {"n": 0}

        def flaky(conn, src, *a, **kw):
            if src.path == self.file_b:
                calls["n"] += 1
                if calls["n"] == 1:
                    real(conn, src, *a, **kw)  # do the inserts, then die before commit
                    raise sqlite3.OperationalError("simulated crash")
            return real(conn, src, *a, **kw)

        with mock.patch.object(ingest, "_index_jsonl", flaky):
            stats = self.sync()
        self.assertEqual(stats.errors, 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.file_b.stem,)), 0)
        self.assertEqual(ingest.inventory(self.conn)["sources_by_status"].get("error"), 1)
        stats = self.sync()
        self.assertEqual(stats.errors, 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.file_b.stem,)), 3)

    def test_original_files_unchanged(self):
        files = sorted(p for p in self.claude.rglob("*") if p.is_file())
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime) for p in files}
        self.sync()
        self.sync(full=True)
        contextpack.build_pack(self.conn, [contextpack.PackItem(1, 2, 2)])
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime)
                 for p in sorted(p for p in self.claude.rglob("*") if p.is_file())}
        self.assertEqual(before, after)

    def test_newer_schema_is_refused(self):
        self.conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            db.migrate(self.conn)


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_global_search_spans_repositories(self):
        res = search.search(self.conn, "token OR migration")
        self.assertEqual({r["project"] for r in res["results"]}, {"repo-alpha", "repo-beta"})

    def test_identifiers_errors_and_phrases(self):
        self.assertGreater(search.search(self.conn, "verify_refresh_token")["total"], 0)
        self.assertGreater(search.search(self.conn, "sqlalchemy.exc.OperationalError")["total"], 0)
        self.assertGreater(search.search(self.conn, '"duplicate column name"')["total"], 0)
        self.assertEqual(search.search(self.conn, '"column duplicate name"')["total"], 0)
        self.assertGreater(search.search(self.conn, "idempot*")["total"], 0)
        self.assertGreater(search.search(self.conn, "auth/tokens.py", mode="substring")["total"], 0)

    def test_filters(self):
        f = search.SearchFilters(project="repo-alpha")
        self.assertTrue(all(r["project"] == "repo-alpha" for r in search.search(self.conn, "token*", f)["results"]))
        f = search.SearchFilters(roles=["tool_result"])
        self.assertTrue(all(r["role"] == "tool_result" for r in search.search(self.conn, "duplicate", f)["results"]))
        self.assertEqual(search.search(self.conn, "duplicate", search.SearchFilters(date_to="2026-02-01"))["total"], 0)
        self.assertGreater(search.search(self.conn, "refresh", search.SearchFilters(file="auth\\tokens.py"))["total"], 0)
        self.assertEqual(search.search(self.conn, "ratelimiter", search.SearchFilters(include_sidechain=False))["total"], 0)

    def test_sorting_and_pagination(self):
        newest = search.search(self.conn, "the OR token OR migration", sort="newest", page_size=2)
        self.assertEqual(len(newest["results"]), 2)
        ts = [r["ts"] for r in newest["results"]]
        self.assertEqual(ts, sorted(ts, reverse=True))
        page2 = search.search(self.conn, "the OR token OR migration", sort="newest", page=2, page_size=2)
        self.assertFalse({r["id"] for r in page2["results"]} & {r["id"] for r in newest["results"]})

    def test_highlighting(self):
        r = search.search(self.conn, "alembic")["results"][0]
        self.assertIn(search.HL_START + "alembic" + search.HL_END, r["snippet"])

    def test_query_builder_never_produces_invalid_fts(self):
        nasty = ['foo(', '"unclosed', 'a OR', 'NOT x', '-x', '()', 'a (b c', 'x:y', "it's", '*', 'C:\\path\\to',
                 'SELECT * FROM t;', '-"a b" c', 'AND OR NOT', '((a))', 'func()', 'a.b.c()', '[x]', '{}']
        for q in nasty:
            res = search.search(self.conn, q)
            self.assertFalse(res["mode"].startswith("substring ("), f"{q!r} -> {res['mode']}")

    def test_context_window(self):
        hit = search.search(self.conn, "idempotent")["results"][0]
        ctx = search.get_context(self.conn, hit["id"], before=2, after=2)
        self.assertEqual(sum(m["is_hit"] for m in ctx["messages"]), 1)
        self.assertEqual(ctx["messages"][-1]["id"], hit["id"])  # last message in its session
        self.assertEqual(len(ctx["messages"]), 3)
        # Fixture directories don't exist on disk, so the session can't be resumed.
        self.assertFalse(ctx["session"]["resumable"])
        self.assertIn("directory", ctx["session"]["resume_blocker"])

    def test_list_sessions_and_projects(self):
        res = search.list_sessions(self.conn)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["sessions"][0]["project"], "repo-beta")  # newest first
        self.assertEqual(search.list_sessions(self.conn, q="JWT")["total"], 1)
        self.assertEqual(len(search.list_projects(self.conn)), 2)


class PackTests(Base):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_pack_provenance_and_redaction(self):
        hit = search.search(self.conn, "api_key")["results"][0]
        pack = contextpack.build_pack(self.conn, [contextpack.PackItem(hit["id"], 3, 0)], task="add auth")
        md = pack["markdown"]
        self.assertIn("not the current repository", md)
        self.assertIn(self.cwd_a, md)
        self.assertIn(self.file_a.stem, md)
        self.assertIn("2026-01-05", md)
        self.assertIn("**Current task:** add auth", md)
        self.assertNotIn("supersecretvalue123", md)
        self.assertGreaterEqual(pack["redactions"], 1)
        self.assertNotIn("consider token rotation", md)  # thinking omitted unless it is the hit
        # Index itself is untouched by redaction.
        self.assertEqual(search.search(self.conn, "supersecretvalue123")["total"], 1)

    def test_pack_budget(self):
        ids = [r["id"] for r in search.search(self.conn, "token OR migration", page_size=10)["results"]]
        pack = contextpack.build_pack(self.conn, [contextpack.PackItem(i, 5, 5) for i in ids], max_tokens=150)
        self.assertLessEqual(pack["tokens"], 260)

    def test_redaction_patterns(self):
        text = ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz ghp_abcdefghijklmnopqrstuvwxyz0123 AKIAABCDEFGHIJKLMNOP "
                "Authorization: Bearer abcdefghijklmnopqrstuvwxyz postgres://admin:hunter22@db/x password=Pa55w0rd!")
        out, n = contextpack.redact(text)
        for secret in ("abcdefghijklmnopqrstuvwxyz", "AKIAABCDEFGHIJKLMNOP", "hunter22", "Pa55w0rd!"):
            self.assertNotIn(secret, out)
        self.assertGreaterEqual(n, 6)


class ResumeTests(Base):
    def setUp(self):
        super().setUp()
        from claude_history import resume
        self.resume = resume
        # Make repo-beta's original directory exist so it is resumable.
        self.real_cwd = self.tmp / "repo-beta"
        self.real_cwd.mkdir()
        self.sync()
        self.conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(self.real_cwd), self.file_b.stem))

    def test_finds_newest_bundled_claude(self):
        home = self.tmp / "home"
        exe = "claude.exe" if sys.platform == "win32" else "claude"
        for ver in ("2.1.9", "2.1.312", "2.1.40"):
            d = home / ".vscode" / "extensions" / f"anthropic.claude-code-{ver}-win32-x64" / "resources" / "native-binary"
            d.mkdir(parents=True)
            (d / exe).write_text("")
        with mock.patch("shutil.which", return_value=None):
            found = self.resume.find_claude(home)
        self.assertIn("2.1.312", found)
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(self.resume.find_claude(self.tmp / "empty-home"))

    def test_plan_success_and_prefix(self):
        p = self.resume.plan(self.conn, self.file_b.stem[:8], claude="claude-bin")
        self.assertEqual(p.session_id, self.file_b.stem)
        self.assertEqual(p.cwd, str(self.real_cwd))
        self.assertEqual(p.argv, ["claude-bin", "--resume", self.file_b.stem])
        self.assertTrue(search.session_meta(self.conn, self.file_b.stem)["resumable"])

    def test_plan_refuses_missing_dir_missing_transcript_and_bad_ids(self):
        with self.assertRaises(self.resume.ResumeError):
            self.resume.plan(self.conn, self.file_a.stem, claude="x")  # directory doesn't exist
        with self.assertRaises(self.resume.ResumeError):
            self.resume.plan(self.conn, "zz; rm -rf /", claude="x")
        self.file_b.unlink()
        self.sync()
        with self.assertRaises(self.resume.ResumeError) as cm:
            self.resume.plan(self.conn, self.file_b.stem, claude="x")
        self.assertIn("claude-history", str(cm.exception))

    def test_run_here_uses_original_directory(self):
        p = self.resume.plan(self.conn, self.file_b.stem, claude="claude-bin")
        with mock.patch("subprocess.call", return_value=0) as call:
            self.assertEqual(self.resume.run_here(p), 0)
        call.assert_called_once_with(["claude-bin", "--resume", self.file_b.stem], cwd=str(self.real_cwd))


class CurationTests(Base):
    def setUp(self):
        super().setUp()
        self.sync()
        self.a, self.b = self.file_a.stem, self.file_b.stem

    def test_archive_hides_everywhere_and_is_reversible(self):
        self.assertGreater(search.search(self.conn, "alembic")["total"], 0)
        self.assertEqual(curation.set_archived(self.conn, [self.b]), 1)
        self.assertEqual(search.search(self.conn, "alembic")["total"], 0)
        self.assertGreater(search.search(self.conn, "alembic", search.SearchFilters(include_archived=True))["total"], 0)
        self.assertEqual([s["id"] for s in search.list_sessions(self.conn)["sessions"]], [self.a])
        self.assertEqual([s["id"] for s in search.list_sessions(self.conn, archived="only")["sessions"]], [self.b])
        self.assertEqual(search.list_sessions(self.conn, archived="all")["total"], 2)
        beta = next(p for p in search.list_projects(self.conn) if p["name"] == "repo-beta")
        self.assertEqual((beta["sessions"], beta["archived"]), (0, 1))
        self.assertEqual(ingest.inventory(self.conn)["sessions_archived"], 1)
        # Still readable by id, and syncing doesn't unarchive it.
        self.assertIsNotNone(search.session_meta(self.conn, self.b)["archived_at"])
        append_jsonl(self.file_b, [rec_user(self.b, "more alembic notes", "2026-03-12T00:00:00Z", self.cwd_b)])
        self.sync()
        self.assertEqual(search.search(self.conn, "alembic")["total"], 0)
        self.assertEqual(curation.set_archived(self.conn, [self.b], archived=False), 1)
        self.assertGreater(search.search(self.conn, "alembic")["total"], 0)

    def test_delete_survives_sync_and_full_rescan(self):
        n_before = self.count()
        n_b = self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.b,))
        self.assertEqual(curation.delete_sessions(self.conn, [self.b]), 1)
        self.assertEqual(self.count(), n_before - n_b)
        self.assertIsNone(search.session_meta(self.conn, self.b))
        self.assertEqual(search.search(self.conn, "alembic")["total"], 0)
        append_jsonl(self.file_b, [rec_user(self.b, "alembic again", "2026-03-12T00:00:00Z", self.cwd_b)])
        self.sync()
        self.sync(full=True)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.b,)), 0)
        self.assertEqual(search.search(self.conn, "alembic")["total"], 0)
        statuses = {r[0] for r in self.conn.execute("SELECT status FROM sources WHERE session_id = ?", (self.b,))}
        self.assertEqual(statuses, {"excluded"})
        self.assertTrue(self.file_b.exists())  # Claude Code's transcript is untouched
        inv = ingest.inventory(self.conn)
        self.assertEqual((inv["sessions_deleted"], inv["sources_by_status"].get("excluded")), (1, 1))
        listed = curation.list_deleted(self.conn)
        self.assertEqual([d["id"] for d in listed], [self.b])
        self.assertTrue(listed[0]["restorable"])
        self.assertEqual(listed[0]["project_name"], "repo-beta")

    def test_restore_reindexes_existing_transcript(self):
        curation.delete_sessions(self.conn, [self.b])
        self.sync()
        res = curation.restore_sessions(self.conn, [self.b])
        self.assertEqual(res, {"restored": [self.b], "unavailable": []})
        self.sync()
        self.assertGreater(search.search(self.conn, "alembic")["total"], 0)
        self.assertEqual(search.session_meta(self.conn, self.b)["message_count"], 3)
        self.assertEqual(curation.list_deleted(self.conn), [])

    def test_restore_when_transcript_is_gone(self):
        curation.delete_sessions(self.conn, [self.b])
        self.file_b.unlink()
        self.sync()
        self.assertFalse(curation.list_deleted(self.conn)[0]["restorable"])
        res = curation.restore_sessions(self.conn, [self.b])
        self.assertEqual(res, {"restored": [], "unavailable": [self.b]})

    def test_subagent_and_memory_of_deleted_session_stay_out(self):
        curation.delete_sessions(self.conn, [self.a])
        self.sync(full=True)
        self.assertEqual(search.search(self.conn, "ratelimiter")["total"], 0)  # subagent transcript of session a
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE session_id = ?", (self.a,)), 0)
        self.assertGreater(search.search(self.conn, "pytest")["total"], 0)  # memory is its own pseudo-session

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            curation.set_archived(self.conn, [])
        self.assertEqual(curation.delete_sessions(self.conn, ["no-such-session"]), 0)

    def test_mcp_excludes_archived_unless_asked(self):
        curation.set_archived(self.conn, [self.b])
        srv = HistoryMCP(self.cfg)
        call = lambda args: srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "search_history", "arguments": args}})["result"]["content"][0]["text"]
        self.assertIn("No matches", call({"query": "alembic"}))
        self.assertIn("repo-beta", call({"query": "alembic", "include_archived": True}))


class McpTests(Base):
    def test_protocol_round_trip(self):
        srv = HistoryMCP(self.cfg)
        init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertIn("search_history", names)
        out = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "search_history", "arguments": {"query": "alembic"}}})
        text = out["result"]["content"][0]["text"]
        self.assertIn("repo-beta", text)
        mid = int(text.split("message_id=")[1].split()[0])
        ctx = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "get_message_context", "arguments": {"message_id": mid}}})
        self.assertIn("MATCH", ctx["result"]["content"][0]["text"])
        bad = srv.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "get_session", "arguments": {}}})
        self.assertTrue(bad["result"]["isError"])
        self.assertEqual(srv.handle({"jsonrpc": "2.0", "id": 6, "method": "nope"})["error"]["code"], -32601)


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.sync()
        from claude_history import server
        self.cfg.port = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.cfg, None))
        self.port = self.httpd.server_address[1]
        # The handler validates Host against cfg.port, so rebuild it with the real port.
        self.cfg.port = self.port
        self.httpd.RequestHandlerClass = server.make_handler(self.cfg, None)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"} | (headers or {})
        if body is not None:
            h["Content-Type"] = "application/json"
            body = json.dumps(body)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)

    def test_api_endpoints(self):
        st, res = self.req("GET", "/api/search?q=alembic")
        self.assertEqual(st, 200)
        self.assertGreater(res["total"], 0)
        st, ctx = self.req("GET", f"/api/context/{res['results'][0]['id']}")
        self.assertEqual(st, 200)
        st, ses = self.req("GET", f"/api/session/{self.file_a.stem}")
        self.assertEqual(ses["session"]["title"], "JWT auth middleware")
        st, pack = self.req("POST", "/api/pack", {"items": [{"message_id": res["results"][0]["id"]}]})
        self.assertEqual(st, 200)
        self.assertIn("Historical context pack", pack["markdown"])
        st, page = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Claude History", page)
        self.assertEqual(self.req("GET", "/../config.py")[0], 404)

    def test_resume_endpoint(self):
        real_cwd = self.tmp / "repo-beta"
        real_cwd.mkdir()
        self.conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(real_cwd), self.file_b.stem))
        from claude_history import resume
        with mock.patch.object(resume, "find_claude", return_value="claude-bin"),                 mock.patch.object(resume, "open_folder_in_vscode") as opened,                 mock.patch.object(resume, "launch_in_new_terminal") as launched:
            st, res = self.req("POST", "/api/resume", {"session_id": self.file_b.stem, "action": "vscode"})
            self.assertEqual(st, 200)
            opened.assert_called_once_with(str(real_cwd))
            self.assertEqual(res["command"], f"cch resume {self.file_b.stem}")
            st, _ = self.req("POST", "/api/resume", {"session_id": self.file_b.stem, "action": "terminal"})
            self.assertEqual(st, 200)
            launched.assert_called_once()
            st, res = self.req("POST", "/api/resume", {"session_id": self.file_a.stem, "action": "vscode"})
            self.assertEqual(st, 409)  # repo-alpha's directory doesn't exist
            self.assertEqual(self.req("POST", "/api/resume", {"session_id": "x", "action": "rm"})[0], 400)
            st, _ = self.req("POST", "/api/resume", {"session_id": self.file_b.stem},
                             headers={"Origin": "http://evil.example"})
            self.assertEqual(st, 403)
            self.assertEqual(opened.call_count, 1)

    def test_archive_delete_restore_endpoints(self):
        b = self.file_b.stem
        st, res = self.req("POST", "/api/sessions/archive", {"ids": [b], "archived": True})
        self.assertEqual((st, res["changed"]), (200, 1))
        self.assertEqual(self.req("GET", "/api/search?q=alembic")[1]["total"], 0)
        self.assertGreater(self.req("GET", "/api/search?q=alembic&archived=1")[1]["total"], 0)
        self.assertEqual(self.req("GET", "/api/sessions?archived=only")[1]["total"], 1)
        self.req("POST", "/api/sessions/archive", {"ids": [b], "archived": False})
        st, res = self.req("POST", "/api/sessions/delete", {"ids": [b]})
        self.assertEqual((st, res["deleted"]), (200, 1))
        self.assertEqual([d["id"] for d in self.req("GET", "/api/deleted")[1]], [b])
        st, res = self.req("POST", "/api/deleted/restore", {"ids": [b]})
        self.assertEqual(res["restored"], [b])
        self.assertGreater(self.req("GET", "/api/search?q=alembic")[1]["total"], 0)  # restored and re-indexed
        self.assertEqual(self.req("POST", "/api/sessions/delete", {"ids": []})[0], 400)
        st, _ = self.req("POST", "/api/sessions/delete", {"ids": [b]}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertIsNotNone(search.session_meta(self.conn, b))  # the cross-origin delete did nothing

    def test_rejects_foreign_host_and_cross_origin_post(self):
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": "evil.example:80"})[0], 403)
        st, _ = self.req("POST", "/api/sync", {"full": False}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
