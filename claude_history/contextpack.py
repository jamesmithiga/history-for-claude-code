"""Context packs: token-conscious Markdown bundles of historical excerpts.

Every excerpt carries its project, working directory, session, and timestamp,
and the pack opens with an explicit notice that it describes *past* work in
*other* locations, so it isn't mistaken for the current repository's state.
Redaction applies only to the exported text; the index is never altered.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import search

# Patterns for likely secrets. Conservative on purpose: false positives only
# hide text in an export, false negatives leak it.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9_\-\.=]{16,}")),
    ("conn-string-password", re.compile(r"(?i)(\b(?:password|pwd)\s*=\s*)[^;'\"\s]+")),
    ("assignment", re.compile(
        r"(?i)(\b(?:api[_-]?key|secret|token|passwd|password|client[_-]?secret|access[_-]?key)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})")),
    ("url-credentials", re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")),
]


def redact(text: str) -> tuple[str, int]:
    count = 0

    def sub(kind: str):
        def _r(m: re.Match) -> str:
            nonlocal count
            count += 1
            label = f"[REDACTED:{kind}]"
            if kind in ("assignment",):
                return m.group(1) + label
            if kind in ("bearer", "conn-string-password"):
                return m.group(1) + label
            if kind == "url-credentials":
                return m.group(1) + label + m.group(3)
            return label
        return _r

    for kind, pat in _SECRET_PATTERNS:
        text = pat.sub(sub(kind), text)
    return text, count


def estimate_tokens(text: str) -> int:
    # ~4 characters per token for English/code; good enough for budgeting.
    return (len(text) + 3) // 4


@dataclass
class PackItem:
    message_id: int
    before: int = 0
    after: int = 0


_ROLE_LABEL = {"user": "User", "assistant": "Claude", "tool_use": "Tool call", "tool_result": "Tool result",
               "thinking": "Claude (thinking)", "summary": "Session summary", "memory": "Memory file"}


def build_pack(conn: sqlite3.Connection, items: list[PackItem], task: str | None = None,
               max_tokens: int = 6000, include_tools: bool = True, do_redact: bool = True,
               tool_char_limit: int = 1200) -> dict:
    # Gather messages per session, merging overlapping windows.
    by_session: dict[str, dict] = {}
    for item in items:
        ctx = search.get_context(conn, item.message_id, item.before, item.after, include_tools)
        if not ctx:
            continue
        sid = ctx["session"]["id"]
        entry = by_session.setdefault(sid, {"session": ctx["session"], "messages": {}, "hits": set()})
        entry["hits"].add(item.message_id)
        for m in ctx["messages"]:
            entry["messages"][m["id"]] = m

    sessions = sorted(by_session.values(), key=lambda e: e["session"].get("started") or "")
    redactions = 0
    blocks: list[str] = []
    for e in sessions:
        s = e["session"]
        head = [f"## {s.get('title') or s['id']}",
                f"- Project: {s.get('project') or '?'}  ",
                f"- Original directory: `{s.get('cwd') or 'unknown'}`"
                + ("" if s.get("cwd_exists") else " (no longer present on this machine)") + "  ",
                f"- Session: `{s['id']}`  ",
                f"- Dates: {(s.get('started') or '?')[:16]} → {(s.get('last_activity') or '?')[:16]} UTC"]
        if s.get("git_branch"):
            head.append(f"- Branch: {s['git_branch']}")
        parts = ["\n".join(head)]
        for m in sorted(e["messages"].values(), key=lambda m: (m["ts"] or "", m["id"])):
            if m["role"] == "thinking" and m["id"] not in e["hits"]:
                continue  # private reasoning is noise in a pack unless it is the match itself
            text = m["text"]
            if m["role"] in ("tool_use", "tool_result") and len(text) > tool_char_limit:
                text = text[:tool_char_limit] + "\n…[truncated]"
            if do_redact:
                text, n = redact(text)
                redactions += n
            label = _ROLE_LABEL.get(m["role"], m["role"])
            if m.get("tool_name") and m["role"] == "tool_use":
                label += f" ({m['tool_name']})"
            mark = " ◀ match" if m["id"] in e["hits"] else ""
            fence = "```" if m["role"] in ("tool_use", "tool_result") else ""
            body = f"{fence}\n{text}\n{fence}" if fence else text
            parts.append(f"**{label}** · {(m['ts'] or '')[:19].replace('T', ' ')} · msg {m['id']}{mark}\n\n{body}")
        blocks.append("\n\n".join(parts))

    header = ["# Historical context pack",
              "",
              "> These excerpts come from **earlier Claude Code sessions in other locations**. They describe "
              "past work and decisions, not the current repository. Verify against the current code before "
              "relying on them. Paths refer to the original directories listed per session.",
              ""]
    if task:
        header += [f"**Current task:** {task}", ""]
    header.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M')} local · {len(sessions)} session(s) · "
                  f"{sum(len(e['messages']) for e in sessions)} message(s)"
                  + (f" · {redactions} secret(s) redacted" if do_redact else " · redaction OFF") + "_")
    header_text = "\n".join(header)

    # Enforce the token budget by dropping whole trailing blocks, then trimming.
    budget_chars = max_tokens * 4
    kept, used, dropped = [], len(header_text), 0
    for b in blocks:
        if used + len(b) + 8 <= budget_chars:
            kept.append(b)
            used += len(b) + 8
        elif not kept:
            kept.append(b[: max(0, budget_chars - used - 40)] + "\n…[truncated to fit token budget]")
            used = budget_chars
        else:
            dropped += 1
    if dropped:
        kept.append(f"_{dropped} further session block(s) omitted to fit the {max_tokens}-token budget._")
    markdown = header_text + "\n\n---\n\n" + "\n\n---\n\n".join(kept) + "\n"
    return {"markdown": markdown, "tokens": estimate_tokens(markdown), "sessions": len(sessions),
            "redactions": redactions, "dropped_blocks": dropped}


def save_pack(markdown: str, packs_dir: Path, name: str | None = None) -> Path:
    packs_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "context-pack").strip("-")[:50] or "context-pack"
    path = packs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def build_session_transcript(conn: sqlite3.Connection, session_id: str, include_tools: bool = True,
                             do_redact: bool = True) -> dict | None:
    """Every stored message of one session, in order, with no further trimming.

    This is what "Send whole session to chat" uses. It works even after Claude
    Code has deleted the original transcript, because it reads the index.
    """
    s = search.session_meta(conn, session_id)
    if not s:
        return None
    roles = "" if include_tools else " AND role IN ('user', 'assistant', 'summary', 'memory')"
    rows = conn.execute(
        f"SELECT id, ts, role, tool_name, text FROM messages WHERE session_id = ? AND is_sidechain = 0{roles}"
        " ORDER BY ts, id", (session_id,)).fetchall()
    redactions = 0
    parts = []
    for m in rows:
        if m["role"] == "thinking":
            continue  # private reasoning isn't part of the conversation to hand over
        text = m["text"]
        if do_redact:
            text, n = redact(text)
            redactions += n
        label = _ROLE_LABEL.get(m["role"], m["role"])
        if m["tool_name"] and m["role"] == "tool_use":
            label += f" ({m['tool_name']})"
        fence = "```" if m["role"] in ("tool_use", "tool_result") else ""
        body = f"{fence}\n{text}\n{fence}" if fence else text
        parts.append(f"**{label}** · {(m['ts'] or '')[:19].replace('T', ' ')}\n\n{body}")
    head = [f"# Full transcript: {s.get('title') or s['id']}",
            "",
            "> This is the complete record of an **earlier Claude Code session in another location**. It describes "
            "past work and decisions, not the current repository. Verify against the current code before relying on it.",
            "",
            f"- Project: {s.get('project') or '?'}",
            f"- Original directory: `{s.get('cwd') or 'unknown'}`" + ("" if s.get("cwd_exists") else " (no longer present)"),
            f"- Session: `{s['id']}`",
            f"- Dates: {(s.get('started') or '?')[:16]} → {(s.get('last_activity') or '?')[:16]} UTC",
            f"- Messages: {len(parts)}" + ("" if include_tools else " (tool calls and output left out)")
            + (f" · {redactions} likely secret(s) redacted" if do_redact and redactions else "")]
    if s.get("source_missing"):
        head.append("- Note: Claude Code has deleted the original transcript; this copy comes from the History index.")
    markdown = "\n".join(head) + "\n\n---\n\n" + "\n\n---\n\n".join(parts) + "\n"
    return {"markdown": markdown, "tokens": estimate_tokens(markdown), "messages": len(parts),
            "redactions": redactions, "session": {k: s.get(k) for k in ("id", "title", "project", "cwd")}}


def save_transcript(markdown: str, exports_dir: Path, session_id: str, title: str | None) -> Path:
    exports_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title or "session").strip("-")[:50] or "session"
    path = exports_dir / f"{slug}-{session_id[:8]}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
