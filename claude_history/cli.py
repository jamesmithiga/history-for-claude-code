"""Command-line interface: `cch <command>` (or `python cch.py <command>`)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, contextpack, curation, db, ingest, search
from .config import Config, load_config

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "cch.py"


def _setup_logging(cfg: Config, verbose: bool, to_stderr: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(cfg.log_path, encoding="utf-8")]
    if to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _hl(s: str) -> str:
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None:
        return s.replace(search.HL_START, "\x1b[1;33m").replace(search.HL_END, "\x1b[0m")
    return s.replace(search.HL_START, "[").replace(search.HL_END, "]")


def cmd_sync(cfg, args):
    stats = ingest.sync(cfg, full=args.full)
    if args.json:
        print(json.dumps(stats.as_dict(), indent=2))
        return
    print(f"Discovered {stats.discovered} sources · updated {stats.indexed} · unchanged {stats.unchanged} · "
          f"errors {stats.errors} · missing {stats.missing} · +{stats.new_messages} messages · {stats.seconds}s")
    for e in stats.error_files:
        print(f"  ERROR {e['path']}: {e['error']}")


def cmd_status(cfg, args):
    conn = db.connect(cfg.db_path)
    inv = ingest.inventory(conn)
    if args.json:
        print(json.dumps(inv, indent=2))
        return
    print(f"Index: {cfg.db_path}")
    print(f"  {inv['projects']} projects · {inv['sessions']} sessions · {inv['messages']:,} messages")
    print(f"  sources: {inv['sources_by_status']}  kinds: {inv['sources_by_kind']}")
    print(f"  sessions whose original file is gone (kept): {inv['sessions_source_missing']}")
    print(f"  malformed lines skipped: {inv['skipped_bad_lines']}")
    print(f"  last sync: {inv['last_sync'] or 'never'} UTC")
    for e in inv["error_sources"]:
        print(f"  ERROR {e['path']}: {e['error']}")


def _filters(args) -> search.SearchFilters:
    return search.SearchFilters(project=args.project, session=getattr(args, "session", None),
                                roles=args.role or [], date_from=args.since, date_to=args.until,
                                file=args.file, include_sidechain=not args.no_subagents)


def cmd_search(cfg, args):
    conn = db.connect(cfg.db_path)
    if not args.no_sync:
        ingest.sync(cfg, conn)
    res = search.search(conn, " ".join(args.query), _filters(args), sort=args.sort, page=args.page,
                        page_size=args.limit, mode="substring" if args.literal else "fts")
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return
    print(f"{res['total']} matches ({res['mode']}), page {res['page']}")
    for r in res["results"]:
        snip = " ".join(r["snippet"].split())
        tool = f"/{r['tool_name']}" if r.get("tool_name") else ""
        print(f"\n#{r['id']}  {r['role']}{tool}  {(r['ts'] or '')[:16]}  {r['project']}  ·  {r['title']}")
        print(f"   {r['cwd']}  ·  session {r['session_id']}")
        print("   " + _hl(snip[:400]))


def _print_msgs(msgs, width):
    for m in msgs:
        mark = "  <== match" if m.get("is_hit") else ""
        tool = f"/{m['tool_name']}" if m.get("tool_name") else ""
        print(f"\n--- [{m['id']}] {m['role']}{tool} · {(m['ts'] or '')[:19]}{mark}")
        t = m["text"]
        print(t if len(t) <= width else t[:width] + f"\n…[+{len(t) - width} chars]")


def _print_session_head(s):
    print(f"{s.get('title') or s['id']}\n  project {s.get('project')} · {s.get('cwd')}"
          + ("" if s.get("cwd_exists") else " (missing)"))
    print(f"  session {s['id']} · {(s.get('started') or '')[:16]} → {(s.get('last_activity') or '')[:16]} UTC"
          f" · {s.get('message_count')} messages")
    if s.get("resume_command"):
        print(f"  resume: {s['resume_command']}")


def cmd_context(cfg, args):
    conn = db.connect(cfg.db_path)
    ctx = search.get_context(conn, args.message_id, args.before, args.after, not args.no_tools)
    if not ctx:
        sys.exit(f"No message {args.message_id}")
    if args.json:
        print(json.dumps(ctx, indent=2, default=str))
        return
    _print_session_head(ctx["session"])
    _print_msgs(ctx["messages"], args.width)


def cmd_show(cfg, args):
    conn = db.connect(cfg.db_path)
    sid = args.session_id
    if len(sid) < 36:  # allow unique prefixes
        rows = conn.execute("SELECT id FROM sessions WHERE id LIKE ?", (sid + "%",)).fetchall()
        if len(rows) != 1:
            sys.exit(f"{len(rows)} sessions match prefix {sid!r}")
        sid = rows[0][0]
    roles = None if args.tools else ["user", "assistant", "summary", "memory"]
    res = search.get_session_messages(conn, sid, args.offset, args.limit, roles)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return
    _print_session_head(res["session"])
    _print_msgs(res["messages"], args.width)


def cmd_sessions(cfg, args):
    conn = db.connect(cfg.db_path)
    res = search.list_sessions(conn, q=args.q, project=args.project, date_from=args.since,
                               date_to=args.until, file=args.file, sort=args.sort, page_size=args.limit,
                               archived="only" if args.archived else "exclude")
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return
    for s in res["sessions"]:
        print(f"{s['id'][:8]}  {(s['last_activity'] or '')[:10]}  {s['message_count']:>5}  "
              f"{(s['project'] or '')[:22]:<22}  {s['title']}" + ("  [source deleted]" if s["source_missing"] else "")
              + ("  [archived]" if s.get("archived_at") else ""))
    print(f"({len(res['sessions'])} of {res['total']})")


def _resolve_ids(conn, refs: list[str], table: str = "sessions") -> list[str]:
    """Accept full ids or unique prefixes (8+ chars are typical)."""
    out = []
    for ref in refs:
        rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ?", (ref + "%",)).fetchall()
        if len(rows) != 1:
            sys.exit(f"{len(rows)} sessions match {ref!r}; give more of the id")
        out.append(rows[0][0])
    return out


def cmd_archive(cfg, args):
    conn = db.connect(cfg.db_path)
    ids = _resolve_ids(conn, args.sessions)
    n = curation.set_archived(conn, ids, archived=not args.undo)
    print(f"{'Unarchived' if args.undo else 'Archived'} {n} session(s). Claude Code's transcripts are untouched.")


def cmd_delete(cfg, args):
    conn = db.connect(cfg.db_path)
    ids = _resolve_ids(conn, args.sessions)
    if not args.yes:
        for sid in ids:
            s = search.session_meta(conn, sid)
            print(f"  {sid}  {s.get('project')}  {s.get('title')}")
        if input(f"Delete {len(ids)} session(s) from the index? Claude Code's transcripts are kept. [y/N] ").lower() != "y":
            sys.exit("Cancelled.")
    n = curation.delete_sessions(conn, ids)
    print(f"Deleted {n} session(s) from the index. Undo with `cch restore <id>` while the transcript still exists.")


def cmd_deleted(cfg, args):
    conn = db.connect(cfg.db_path)
    rows = curation.list_deleted(conn)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['id'][:8]}  deleted {r['deleted_at'][:10]}  {(r['project_name'] or '')[:22]:<22}  {r['title']}"
              + ("" if r["restorable"] else "  [transcript gone: not restorable]"))
    print(f"({len(rows)} deleted from the index)")


def cmd_restore(cfg, args):
    conn = db.connect(cfg.db_path)
    ids = _resolve_ids(conn, args.sessions, table="deleted_sessions")
    res = curation.restore_sessions(conn, ids)
    if res["restored"]:
        ingest.sync(cfg, conn)
    print(f"Restored {len(res['restored'])} session(s).")
    for sid in res["unavailable"]:
        print(f"  {sid}: Claude Code no longer has its transcript, so there is nothing to re-index.")


def cmd_projects(cfg, args):
    conn = db.connect(cfg.db_path)
    for p in search.list_projects(conn):
        print(f"{p['sessions']:>4} sessions  {p['messages']:>7} msgs  {p['name']:<28} {p['cwd'] or p['key']}")


def cmd_pack(cfg, args):
    conn = db.connect(cfg.db_path)
    items = [contextpack.PackItem(i, args.before, args.after) for i in args.message_ids]
    pack = contextpack.build_pack(conn, items, task=args.task, max_tokens=args.budget,
                                  include_tools=not args.no_tools, do_redact=not args.no_redact)
    if args.out:
        Path(args.out).write_text(pack["markdown"], encoding="utf-8")
        print(f"Wrote {args.out} (≈{pack['tokens']} tokens, {pack['redactions']} redactions)", file=sys.stderr)
    elif args.save:
        path = contextpack.save_pack(pack["markdown"], cfg.packs_dir, args.task)
        print(f"Saved {path} (≈{pack['tokens']} tokens)", file=sys.stderr)
    if args.copy:
        _to_clipboard(pack["markdown"])
        print(f"Copied to clipboard (≈{pack['tokens']} tokens, {pack['redactions']} redactions)", file=sys.stderr)
    if not (args.out or args.save or args.copy):
        print(pack["markdown"])


def _to_clipboard(text: str) -> None:
    if os.name == "nt":
        # clip.exe expects UTF-16LE for non-ASCII text.
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
    elif shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    elif shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
    else:
        raise SystemExit("No clipboard tool found; use --out instead.")


def cmd_resume(cfg, args):
    from . import resume
    conn = db.connect(cfg.db_path)
    try:
        p = resume.plan(conn, args.session)
    except resume.ResumeError as exc:
        sys.exit(str(exc))
    if args.print:
        print(p.command)
        return
    print(f"Resuming: {p.title or p.session_id}\n  in {p.cwd}\n  with {p.claude}", file=sys.stderr)
    if args.vscode:
        resume.open_folder_in_vscode(p.cwd)
        print("  opened the folder in VS Code (its Claude Code history list will show this session)",
              file=sys.stderr)
    if args.new_window:
        resume.launch_in_new_terminal(p)
        return
    if args.vscode and not args.here:
        return
    sys.exit(resume.run_here(p))


def cmd_export(cfg, args):
    conn = db.connect(cfg.db_path)
    sid = _resolve_ids(conn, [args.session])[0]
    t = contextpack.build_session_transcript(conn, sid, include_tools=not args.no_tools, do_redact=not args.no_redact)
    if args.out:
        Path(args.out).write_text(t["markdown"], encoding="utf-8")
        where = args.out
    else:
        where = str(contextpack.save_transcript(t["markdown"], cfg.exports_dir, sid, t["session"]["title"]))
    if args.copy:
        _to_clipboard(t["markdown"])
    print(f"{t['messages']} messages, ≈{t['tokens']:,} tokens, {t['redactions']} redaction(s) → {where}"
          + (" (also copied to the clipboard)" if args.copy else ""))


def cmd_serve(cfg, args):
    from . import server
    if args.port:
        cfg.port = args.port
    server.serve(cfg, open_browser=args.open, background_sync=not args.no_sync)


def cmd_mcp(cfg, args):
    from . import mcp_server
    mcp_server.run(cfg)


def _claude_json_path() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) / ".claude.json" if env else Path.home() / ".claude.json"


def _launch_path(p: str) -> str:
    """A path Claude Code can launch: on Windows it splits commands at spaces
    (seen with "C:\\Users\\First Last\\..."), so use the 8.3 short form there."""
    if os.name != "nt" or " " not in p:
        return p
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf)) and " " not in buf.value:
        return buf.value
    print(f"Warning: {p} contains spaces and has no short (8.3) name; Claude Code may fail to start it.",
          file=sys.stderr)
    return p


def cmd_install_mcp(cfg, args):
    """Register the MCP server at user scope (all projects) in ~/.claude.json."""
    command, script = _launch_path(sys.executable), _launch_path(str(ENTRY))
    # The owner tag lets the VS Code extension recognise (and re-point after updates)
    # only the registrations it created.
    env = {"CLAUDE_HISTORY_OWNER": args.owner} if args.owner else {}
    entry = {"type": "stdio", "command": command, "args": [script, "mcp"], "env": env}
    path = _claude_json_path()
    if args.print_only:
        print(json.dumps({"mcpServers": {"claude-history": entry}}, indent=2))
        return
    if shutil.which("claude"):
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", "claude-history"],
                       capture_output=True)
        env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", *env_args, "claude-history", "--",
                            command, script, "mcp"])
        sys.exit(r.returncode)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if path.exists():
        shutil.copy2(path, backup)
    data.setdefault("mcpServers", {})["claude-history"] = entry
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    print(f"Registered 'claude-history' MCP server (user scope) in {path}")
    if path.exists():
        print(f"Backup of previous file: {backup}")
    print("Start a new Claude Code session (any repo) and run /mcp to confirm it is connected.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cch", description="Global Claude Code history: search and reuse past sessions from any repo.")
    p.add_argument("--config", help="Path to config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def filt(sp):
        sp.add_argument("--project", "-p", help="Project name, key or directory substring")
        sp.add_argument("--role", "-r", action="append", choices=search.ROLES)
        sp.add_argument("--since", help="YYYY-MM-DD")
        sp.add_argument("--until", help="YYYY-MM-DD")
        sp.add_argument("--file", "-f", help="Referenced file path contains")
        sp.add_argument("--no-subagents", action="store_true")

    s = sub.add_parser("sync", help="Incremental index update")
    s.add_argument("--full", action="store_true", help="Re-read every source from scratch")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("status", help="Index inventory and health")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("search", help="Search all history")
    s.add_argument("query", nargs="+")
    filt(s)
    s.add_argument("--session")
    s.add_argument("--sort", choices=["relevance", "newest", "oldest"], default="relevance")
    s.add_argument("--limit", "-n", type=int, default=10)
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--literal", "-l", action="store_true", help="Literal substring match")
    s.add_argument("--no-sync", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("context", help="Show a message with surrounding conversation")
    s.add_argument("message_id", type=int)
    s.add_argument("--before", "-B", type=int, default=4)
    s.add_argument("--after", "-A", type=int, default=4)
    s.add_argument("--no-tools", action="store_true")
    s.add_argument("--width", type=int, default=3000)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("show", help="Print a session (id or unique prefix)")
    s.add_argument("session_id")
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--tools", action="store_true", help="Include tool calls and output")
    s.add_argument("--width", type=int, default=3000)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("sessions", help="List sessions across all projects")
    s.add_argument("q", nargs="?")
    s.add_argument("--project", "-p")
    s.add_argument("--since")
    s.add_argument("--until")
    s.add_argument("--file", "-f")
    s.add_argument("--sort", choices=["newest", "oldest", "messages", "title"], default="newest")
    s.add_argument("--limit", "-n", type=int, default=30)
    s.add_argument("--archived", action="store_true", help="List archived sessions instead")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("archive", help="Hide sessions from search and lists (reversible)")
    s.add_argument("sessions", nargs="+", help="Session ids or unique prefixes")
    s.add_argument("--undo", action="store_true", help="Unarchive instead")
    s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("delete", help="Remove sessions from the index (transcripts are kept)")
    s.add_argument("sessions", nargs="+", help="Session ids or unique prefixes")
    s.add_argument("--yes", "-y", action="store_true", help="Don't ask for confirmation")
    s.set_defaults(fn=cmd_delete)

    s = sub.add_parser("deleted", help="List sessions deleted from the index")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_deleted)

    s = sub.add_parser("restore", help="Bring deleted sessions back into the index")
    s.add_argument("sessions", nargs="+", help="Session ids or unique prefixes")
    s.set_defaults(fn=cmd_restore)

    s = sub.add_parser("projects", help="List projects with history")
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("pack", help="Build a context pack from message ids")
    s.add_argument("message_ids", type=int, nargs="+")
    s.add_argument("--before", "-B", type=int, default=2)
    s.add_argument("--after", "-A", type=int, default=2)
    s.add_argument("--task")
    s.add_argument("--budget", type=int, default=6000, help="Token budget")
    s.add_argument("--no-tools", action="store_true")
    s.add_argument("--no-redact", action="store_true")
    s.add_argument("--out", "-o")
    s.add_argument("--save", action="store_true", help="Save into the packs folder")
    s.add_argument("--copy", "-c", action="store_true", help="Copy to clipboard")
    s.set_defaults(fn=cmd_pack)

    s = sub.add_parser("resume", help="Continue a past session in Claude Code (from its original folder)",
                       description="By default runs `claude --resume` in THIS terminal (use it in VS Code's "
                                   "integrated terminal). --vscode opens the session's folder in VS Code instead.")
    s.add_argument("session", help="Session id or unique prefix")
    s.add_argument("--vscode", action="store_true", help="Open the session's folder in VS Code")
    s.add_argument("--here", action="store_true", help="With --vscode, also resume in this terminal")
    s.add_argument("--new-window", action="store_true", help="Resume in a new console window")
    s.add_argument("--print", action="store_true", help="Only print the command")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("export", help="Write a whole session (from the index) as Markdown")
    s.add_argument("session", help="Session id or unique prefix")
    s.add_argument("--out", "-o", help="File to write (default: the exports folder)")
    s.add_argument("--no-tools", action="store_true", help="Leave out tool calls and output")
    s.add_argument("--no-redact", action="store_true")
    s.add_argument("--copy", "-c", action="store_true", help="Also copy it to the clipboard")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("serve", help="Run the local web app")
    s.add_argument("--port", type=int)
    s.add_argument("--open", action="store_true", help="Open the browser")
    s.add_argument("--no-sync", action="store_true", help="Disable background sync")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp", help="Run the MCP stdio server (used by Claude Code)")
    s.set_defaults(fn=cmd_mcp)

    s = sub.add_parser("install-mcp", help="Register the MCP server for all Claude Code projects")
    s.add_argument("--print-only", action="store_true", help="Print the config instead of writing it")
    s.add_argument("--owner", help=argparse.SUPPRESS)  # set by the VS Code extension
    s.set_defaults(fn=cmd_install_mcp)
    return p


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    _setup_logging(cfg, args.verbose, to_stderr=args.cmd in ("serve",) or args.verbose)
    args.fn(cfg, args)
