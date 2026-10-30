"""Local web app + JSON API (stdlib only).

Security posture:
- Binds to 127.0.0.1 by default; never 0.0.0.0 unless the config says so.
- Rejects requests whose Host header isn't this loopback address (blocks DNS
  rebinding) and cross-origin POSTs (no CORS headers are ever sent).
- Request logs record method and path only, never query strings (which hold
  search terms).
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import socketserver
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, contextpack, curation, db, ingest, search
from .config import Config

log = logging.getLogger("claude_history.server")
WEB_DIR = Path(__file__).parent / "web"


class SyncWorker(threading.Thread):
    """Background incremental sync (polling: stdlib has no portable file watcher)."""

    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="sync")
        self.cfg = cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.full_requested = False
        self.running = False
        self.last_error: str | None = None

    def request(self, full: bool = False) -> None:
        self.full_requested |= full
        self.wake.set()

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        while True:
            full, self.full_requested = self.full_requested, False
            with self.lock:
                self.running = True
                try:
                    stats = ingest.sync(self.cfg, conn, full=full)
                    self.last_error = None
                    if stats.indexed:
                        log.info("sync: %d sources updated, %d new messages in %.2fs",
                                 stats.indexed, stats.new_messages, stats.seconds)
                except Exception as exc:  # keep the worker alive; surface in /api/status
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.exception("sync failed")
                finally:
                    self.running = False
            self.wake.wait(self.cfg.sync_interval)
            self.wake.clear()


def _int(qs: dict, key: str, default: int) -> int:
    try:
        return int(qs.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def _str(qs: dict, key: str) -> str | None:
    v = qs.get(key, [""])[0].strip()
    return v or None


class LocalHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse-DNS lookup in server_bind, which
    can stall startup for many seconds on macOS. We only ever bind loopback."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def make_handler(cfg: Config, worker: SyncWorker | None, on_shutdown=None):
    allowed_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}",
                     f"{cfg.host}:{cfg.port}"}
    local = threading.local()

    def conn():
        if getattr(local, "conn", None) is None:
            local.conn = db.connect(cfg.db_path)
        return local.conn

    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-history"

        def log_message(self, fmt, *args):  # path only, no query string
            log.debug("%s %s", self.command, urlsplit(self.path).path)

        # ---------------------------------------------------------- helpers --
        def _guard(self) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
                return False
            return True

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel: str):
            rel = rel.lstrip("/") or "index.html"
            path = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in path.parents or not path.is_file():
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                self.close_connection = True
                raise ValueError("body too large")
            # Read the body before any rejection: closing a socket with unread data
            # makes Windows reset the connection instead of delivering our 4xx.
            raw = self.rfile.read(n)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ValueError("expected application/json")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc not in allowed_hosts:
                raise PermissionError("cross-origin request")
            return json.loads(raw or b"{}")

        # ----------------------------------------------------------- routes --
        def do_GET(self):
            if not self._guard():
                return
            u = urlsplit(self.path)
            qs = parse_qs(u.query)
            p = u.path
            try:
                if p == "/api/status":
                    inv = ingest.inventory(conn())
                    inv["sync_running"] = bool(worker and worker.running)
                    inv["sync_error"] = worker.last_error if worker else None
                    inv["sync_interval"] = cfg.sync_interval
                    inv["db_path"] = str(cfg.db_path)
                    # Lets a newer client recognise (and replace) an older server on the shared port.
                    inv["app_version"] = __version__
                    inv["pid"] = os.getpid()
                    inv["claude_dirs"] = [str(d) for d in cfg.claude_dirs]
                    return self._json(inv)
                if p == "/api/search":
                    f = search.SearchFilters(
                        project=_str(qs, "project"), session=_str(qs, "session"),
                        roles=[r for r in (_str(qs, "roles") or "").split(",") if r],
                        date_from=_str(qs, "from"), date_to=_str(qs, "to"), file=_str(qs, "file"),
                        include_sidechain=_str(qs, "sidechain") != "0",
                        include_memory=_str(qs, "memory") != "0",
                        include_archived=_str(qs, "archived") == "1")
                    t0 = time.perf_counter()
                    res = search.search(conn(), _str(qs, "q") or "", f, sort=_str(qs, "sort") or "relevance",
                                        page=_int(qs, "page", 1), page_size=_int(qs, "page_size", 30),
                                        mode=_str(qs, "mode") or "fts")
                    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
                    return self._json(res)
                if p == "/api/sessions" and _str(qs, "match"):
                    arch = _str(qs, "archived") or "exclude"
                    f = search.SearchFilters(
                        project=_str(qs, "project"), date_from=_str(qs, "from"), date_to=_str(qs, "to"),
                        file=_str(qs, "file"), roles=[r for r in (_str(qs, "roles") or "").split(",") if r],
                        include_sidechain=_str(qs, "sidechain") != "0", include_memory=False,
                        include_archived=arch != "exclude")
                    res = search.sessions_matching(conn(), _str(qs, "match"), f, sort=_str(qs, "sort") or "relevance",
                                                   page=_int(qs, "page", 1), page_size=_int(qs, "page_size", 50))
                    if arch == "only":  # rare; filter after grouping rather than add another SQL variant
                        res["sessions"] = [s for s in res["sessions"] if s["archived_at"]]
                        res["total"] = len(res["sessions"])
                    return self._json(res)
                if p == "/api/sessions":
                    return self._json(search.list_sessions(
                        conn(), q=_str(qs, "q"), project=_str(qs, "project"), date_from=_str(qs, "from"),
                        date_to=_str(qs, "to"), file=_str(qs, "file"), sort=_str(qs, "sort") or "newest",
                        page=_int(qs, "page", 1), page_size=_int(qs, "page_size", 50),
                        include_memory=_str(qs, "memory") == "1",
                        archived=_str(qs, "archived") or "exclude"))
                if p == "/api/deleted":
                    return self._json(curation.list_deleted(conn()))
                if p == "/api/projects":
                    return self._json(search.list_projects(conn()))
                if p.startswith("/api/session/"):
                    sid = unquote(p.removeprefix("/api/session/"))
                    res = search.get_session_messages(
                        conn(), sid, offset=_int(qs, "offset", 0), limit=_int(qs, "limit", 400),
                        roles=[r for r in (_str(qs, "roles") or "").split(",") if r] or None,
                        include_sidechain=_str(qs, "sidechain") == "1")
                    if not res["session"]:
                        return self._json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                    return self._json(res)
                if p.startswith("/api/context/"):
                    res = search.get_context(conn(), int(p.removeprefix("/api/context/")),
                                             before=_int(qs, "before", 4), after=_int(qs, "after", 4),
                                             include_tools=_str(qs, "tools") != "0")
                    if not res:
                        return self._json({"error": "message not found"}, HTTPStatus.NOT_FOUND)
                    return self._json(res)
                if p.startswith("/api/"):
                    return self._json({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
                return self._static(p)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                log.exception("GET %s failed", p)
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            if not self._guard():
                return
            p = urlsplit(self.path).path
            try:
                body = self._body()
                if p == "/api/shutdown":
                    if not on_shutdown:
                        return self._json({"error": "shutdown not supported here"}, HTTPStatus.NOT_FOUND)
                    self._json({"stopping": True, "app_version": __version__})
                    threading.Thread(target=on_shutdown, daemon=True).start()
                    return
                if p == "/api/sync":
                    full = bool(body.get("full"))
                    if worker:
                        worker.request(full)
                        return self._json({"queued": True, "full": full})
                    stats = ingest.sync(cfg, conn(), full=full)
                    return self._json(stats.as_dict())
                if p == "/api/sessions/archive":
                    n = curation.set_archived(conn(), body.get("ids"), bool(body.get("archived", True)))
                    return self._json({"changed": n})
                if p == "/api/sessions/delete":
                    return self._json({"deleted": curation.delete_sessions(conn(), body.get("ids"))})
                if p == "/api/deleted/restore":
                    res = curation.restore_sessions(conn(), body.get("ids"))
                    if res["restored"]:
                        ingest.sync(cfg, conn())  # re-read the transcripts now, not on the next tick
                    return self._json(res)
                if p == "/api/resume":
                    from . import resume
                    action = body.get("action", "vscode")
                    if action not in ("vscode", "terminal"):
                        raise ValueError("action must be 'vscode' or 'terminal'")
                    try:
                        # Only ids already in the index, run from their recorded directory.
                        plan = resume.plan(conn(), str(body.get("session_id", "")))
                    except resume.ResumeError as exc:
                        return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                    if action == "vscode":
                        resume.open_folder_in_vscode(plan.cwd)
                    else:
                        resume.launch_in_new_terminal(plan)
                    return self._json({"ok": True, "action": action, "cwd": plan.cwd,
                                       "session_id": plan.session_id, "command": f"cch resume {plan.session_id}"})
                if p == "/api/session-export":
                    t = contextpack.build_session_transcript(
                        conn(), str(body.get("session_id", "")), include_tools=bool(body.get("include_tools", True)),
                        do_redact=bool(body.get("redact", cfg.redact_packs)))
                    if not t:
                        return self._json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                    path = contextpack.save_transcript(t["markdown"], cfg.exports_dir, t["session"]["id"],
                                                       t["session"]["title"])
                    inline = t["tokens"] <= cfg.whole_session_inline_tokens
                    return self._json({"path": str(path), "tokens": t["tokens"], "messages": t["messages"],
                                       "redactions": t["redactions"], "inline": inline,
                                       "markdown": t["markdown"] if inline or body.get("want_markdown") else None,
                                       "session": t["session"]})
                if p == "/api/pack":
                    items = [contextpack.PackItem(int(i["message_id"]), int(i.get("before", 0)),
                                                  int(i.get("after", 0))) for i in body.get("items", [])]
                    if not items:
                        raise ValueError("no items")
                    pack = contextpack.build_pack(
                        conn(), items, task=body.get("task") or None,
                        max_tokens=int(body.get("max_tokens") or 6000),
                        include_tools=bool(body.get("include_tools", True)),
                        do_redact=bool(body.get("redact", cfg.redact_packs)))
                    if body.get("save"):
                        pack["saved_to"] = str(contextpack.save_pack(pack["markdown"], cfg.packs_dir,
                                                                      body.get("task")))
                    return self._json(pack)
                return self._json({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
            except (ValueError, KeyError, TypeError) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                log.exception("POST %s failed", p)
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def serve(cfg: Config, open_browser: bool = False, background_sync: bool = True) -> None:
    worker = SyncWorker(cfg) if background_sync else None
    if worker:
        worker.start()
    httpd = None

    def shutdown() -> None:
        log.info("shutdown requested by a client")
        httpd.shutdown()

    httpd = LocalHTTPServer((cfg.host, cfg.port), make_handler(cfg, worker, on_shutdown=shutdown))
    url = f"http://127.0.0.1:{cfg.port}/" if cfg.host in ("127.0.0.1", "0.0.0.0") else f"http://{cfg.host}:{cfg.port}/"
    print(f"Claude History running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
