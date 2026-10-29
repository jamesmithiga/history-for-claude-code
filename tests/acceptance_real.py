"""Acceptance checks + benchmarks against the REAL local Claude Code history.

Uses a throwaway index (so the measured backfill is a true cold start) and
never writes to the Claude directory. Prints a Markdown report.

Usage: python tests/acceptance_real.py [--nonce TOKEN] [--out report.md]
  --nonce: a unique string that appeared in a recent Claude Code conversation,
           to verify that brand-new conversation content becomes searchable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_history import contextpack, db, ingest, search  # noqa: E402
from claude_history.config import Config  # noqa: E402


def fingerprint(projects: Path) -> dict[str, tuple[int, str]]:
    out = {}
    for p in projects.rglob("*"):
        if p.is_file() and p.suffix in (".jsonl", ".md"):
            data = p.read_bytes()
            out[str(p)] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nonce")
    ap.add_argument("--out")
    args = ap.parse_args()

    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = claude_dir / "projects"
    tmp = Path(tempfile.mkdtemp(prefix="cch-accept-"))
    cfg = Config(data_dir=tmp, claude_dirs=[claude_dir])
    results: list[tuple[str, bool, str]] = []
    lines: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    before = fingerprint(projects)
    raw_bytes = sum(v[0] for v in before.values())

    # ---------------------------------------------------------- backfill --
    t0 = time.perf_counter()
    stats = ingest.sync(cfg)
    backfill = time.perf_counter() - t0
    conn = db.connect(cfg.db_path)
    inv = ingest.inventory(conn)
    t0 = time.perf_counter()
    stats2 = ingest.sync(cfg, conn)
    incremental = time.perf_counter() - t0
    t0 = time.perf_counter()
    ingest.sync(cfg, conn, full=True)
    full_rescan = time.perf_counter() - t0
    inv2 = ingest.inventory(conn)

    # ----------------------------------------------------------- queries --
    queries = ["openpyxl", "ModuleNotFoundError", "load_workbook", '"No module named"', "audit report",
               "pyodbc OR pymssql", "migrat*", "signature pdf", "error -openpyxl", "invoice", "Traceback",
               "schema", "python", "the"]
    timings, totals = [], {}
    for q in queries:
        for sort in ("relevance", "newest"):
            t0 = time.perf_counter()
            r = search.search(conn, q, sort=sort, page_size=25)
            timings.append((time.perf_counter() - t0) * 1000)
            totals[q] = r["total"]
    t0 = time.perf_counter()
    search.search(conn, "load_workbook", mode="substring")
    substring_ms = (time.perf_counter() - t0) * 1000

    # ---------------------------------------------------- acceptance 1..12 --
    sessions = search.list_sessions(conn, page_size=500)["sessions"]
    by_project: dict[str, list] = {}
    for s in sessions:
        by_project.setdefault(s["project"], []).append(s)

    # 1 & 12: launched from an unrelated directory, find another repo's session.
    target = max(sessions, key=lambda s: s["message_count"])
    probe_text = conn.execute(
        "SELECT text FROM messages WHERE session_id = ? AND role = 'user' ORDER BY ts LIMIT 1",
        (target["id"],)).fetchone()[0]
    probe = " ".join(w for w in probe_text.split()[:12] if w.isalpha() and len(w) > 4)[:80] or target["title"]
    unrelated = Path(tempfile.mkdtemp(prefix="unrelated-repo-"))
    env = os.environ | {"CLAUDE_HISTORY_DATA": str(tmp), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(ROOT / "cch.py"), "search", "--json", "--no-sync", "-n", "50",
                           *probe.split()[:4]], cwd=unrelated, env=env, capture_output=True, text=True,
                          encoding="utf-8")
    found = proc.returncode == 0 and any(r["session_id"] == target["id"]
                                         for r in json.loads(proc.stdout)["results"])
    check("1. Session from repo A found while working in unrelated dir B", found,
          f"cwd={unrelated}; probe={probe.split()[:4]} → session {target['id'][:8]} ({target['project']})")

    # 2: everything indexed predates this install.
    oldest = min((s["started"] for s in sessions if s["started"]), default=None)
    check("2. Pre-existing conversations indexed", inv["sessions"] > 0 and oldest is not None,
          f"{inv['sessions']} sessions, oldest from {oldest}")

    # 3: global search spans repositories.
    multi = search.search(conn, "openpyxl", page_size=200)
    projs = {r["project"] for r in multi["results"]}
    check("3. One query returns matches from multiple repositories", len(projs) >= 2,
          f"'openpyxl' → {multi['total']} matches across {len(projs)} projects")

    # 4: function name / error message.
    fn = search.search(conn, "load_workbook")
    err = search.search(conn, '"No module named"')
    check("4. Function name and error message located", fn["total"] > 0 and err["total"] > 0,
          f"load_workbook: {fn['total']}, \"No module named\": {err['total']}")

    # 5: context.
    ctx = search.get_context(conn, err["results"][0]["id"], 3, 3)
    check("5. Result opens with surrounding context", ctx and len(ctx["messages"]) > 1
          and sum(m["is_hit"] for m in ctx["messages"]) == 1,
          f"{len(ctx['messages'])} messages around hit, session '{ctx['session']['title']}'")

    # 6: no duplicates.
    dupes = conn.execute("SELECT COUNT(*) FROM (SELECT session_id, uuid, part FROM messages "
                         "GROUP BY 1,2,3 HAVING COUNT(*) > 1)").fetchone()[0]
    check("6. Repeated sync creates no duplicates",
          stats2.new_messages == 0 and dupes == 0 and inv2["messages"] == inv["messages"],
          f"2nd sync +{stats2.new_messages}; full rescan {inv['messages']}→{inv2['messages']}; duplicate keys: {dupes}")

    # 7: new conversation content becomes searchable.
    if args.nonce:
        hits = search.search(conn, args.nonce, mode="substring")["total"]
        check("7. New conversation content searchable after sync", hits > 0,
              f"nonce '{args.nonce}' found {hits}x")
    else:
        check("7. New conversation content searchable after sync", stats.new_messages > 0,
              "covered by unit test test_new_session_becomes_searchable; pass --nonce for a live check")

    # 8: missing repository.
    gone = [s for s in sessions if s["cwd"] and not Path(s["cwd"]).exists()]
    memory_only = [p for p in search.list_projects(conn) if not p["sessions"]]
    detail = (f"{len(gone)} sessions whose directory is gone; all readable" if gone else
              "no session directory is currently missing on this machine; covered by unit test "
              "test_deleted_source_keeps_history")
    ok8 = all(search.get_session_messages(conn, s["id"], 0, 5)["messages"] for s in gone)
    check("8. Missing repo/transcript does not hide indexed history", ok8,
          detail + f"; {len(memory_only)} projects survive only as memory files (transcripts purged by Claude Code)")

    # 9: pack provenance.
    pack = contextpack.build_pack(conn, [contextpack.PackItem(fn["results"][0]["id"], 2, 2),
                                         contextpack.PackItem(err["results"][0]["id"], 1, 1)])
    md = pack["markdown"]
    ok9 = "not the current repository" in md and "Session: `" in md and "Original directory" in md
    check("9. Context pack keeps provenance", ok9,
          f"≈{pack['tokens']} tokens, {pack['sessions']} sessions, {pack['redactions']} redactions")

    # 10: no remote API.
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "claude_history").rglob("*.py"))
    remote = [m for m in ("urllib.request", "requests", "httpx", "anthropic", "openai", "socket.create_connection")
              if f"import {m}" in src or f"from {m}" in src]
    check("10. Works with no remote AI API", not remote, "no outbound network imports" if not remote else str(remote))

    # 11: originals unchanged (active sessions may grow; existing bytes must be identical).
    after = fingerprint(projects)
    changed = []
    for path, (size, digest) in before.items():
        if path not in after:
            continue  # deleted by Claude Code itself during the run
        if after[path] != (size, digest):
            with open(path, "rb") as fh:
                prefix = fh.read(size)
            if hashlib.sha256(prefix).hexdigest() != digest:
                changed.append(path)
    grew = sum(1 for p, v in before.items() if p in after and after[p] != v)
    check("11. Original history files unchanged", not changed,
          f"{len(before)} files hashed; {grew} grew by appends from live sessions (prefix verified); "
          f"{len(changed)} modified")

    check("12. Usable from a workspace unrelated to the source conversation", found,
          "same run as #1: CLI launched from an empty temp directory")

    # ------------------------------------------------------------ report --
    db_size = cfg.db_path.stat().st_size
    timings.sort()
    lines += [
        "# Acceptance results (real local history)",
        "",
        f"_Run {time.strftime('%Y-%m-%d %H:%M')} · {platform.system()} {platform.release()} · "
        f"Python {platform.python_version()} · SQLite {db.sqlite3.sqlite_version}_",
        "",
        "## Data measured",
        f"- History dir: `{projects}`",
        f"- {len(before)} transcript/memory files, {raw_bytes / 1e6:.1f} MB raw",
        f"- Indexed: {inv['projects']} projects, {inv['sessions']} sessions, {inv['messages']:,} messages; "
        f"sources {inv['sources_by_kind']}; malformed lines {inv['skipped_bad_lines']}; errors {stats.errors}",
        f"- Index size: {db_size / 1e6:.1f} MB",
        "",
        "## Performance (measured)",
        f"- Cold backfill: **{backfill:.2f}s** ({raw_bytes / 1e6 / backfill:.0f} MB/s)",
        f"- Incremental sync, nothing changed: **{incremental * 1000:.0f} ms**",
        f"- Full rescan: **{full_rescan:.2f}s**",
        f"- FTS search ({len(timings)} queries, page of 25 incl. COUNT): median **{statistics.median(timings):.1f} ms**, "
        f"p95 {timings[int(len(timings) * .95) - 1]:.1f} ms, max {timings[-1]:.1f} ms",
        f"- Literal substring scan: {substring_ms:.1f} ms",
        "",
        "| Query | Matches |", "|---|---:|", *[f"| `{q}` | {n:,} |" for q, n in totals.items()],
        "",
        "## Acceptance tests",
        "| # | Scenario | Result | Evidence |", "|---|---|---|---|",
        *[f"| {n.split('.')[0]} | {n.split('. ', 1)[1]} | {'PASS' if ok else '**FAIL**'} | {d} |" for n, ok, d in results],
    ]
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(unrelated, ignore_errors=True)
    sys.exit(0 if all(ok for _, ok, _ in results) else 1)


if __name__ == "__main__":
    main()
