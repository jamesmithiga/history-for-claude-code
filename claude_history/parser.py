"""Turn Claude Code JSONL records into searchable message rows.

Validated against real transcripts (see docs/history-format.md). Unknown record
types and block types are ignored rather than rejected, so format additions in
newer Claude Code versions don't break ingestion.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

PARSER_VERSION = 4

# Records that carry conversation content. Everything else (api-request blobs,
# queue operations, file-history snapshots...) is bookkeeping and is skipped.
_CONTENT_TYPES = {"user", "assistant", "summary"}
_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
# Wrapper tags around pasted text: keep the text, drop the markup.
_PASTE_TAG_RE = re.compile(r"</?pasted_content[^>]*>")
_FILE_KEYS = ("file_path", "path", "notebook_path")


@dataclass
class ParsedMessage:
    uuid: str
    part: int
    ts: str | None
    role: str
    text: str
    tool_name: str | None = None
    is_sidechain: bool = False
    is_error: bool = False
    files: list[str] = field(default_factory=list)


@dataclass
class RecordResult:
    """Everything one JSONL line contributes."""
    session_id: str | None = None
    messages: list[ParsedMessage] = field(default_factory=list)
    title: str | None = None          # ai-title (auto) or custom-title (user-set)
    title_is_custom: bool = False
    cwd: str | None = None
    git_branch: str | None = None
    entrypoint: str | None = None
    version: str | None = None


@dataclass
class Limits:
    max_text_chars: int = 60000
    max_tool_result_chars: int = 6000
    max_tool_input_chars: int = 3000


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _clean_user_text(text: str) -> str:
    return _PASTE_TAG_RE.sub("", _REMINDER_RE.sub("", text)).strip()


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif block.get("type") == "image":
                parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return ""


def _tool_input_files(inp: Any) -> list[str]:
    if not isinstance(inp, dict):
        return []
    files = [inp[k] for k in _FILE_KEYS if isinstance(inp.get(k), str) and inp[k]]
    for edit in inp.get("edits") or []:
        if isinstance(edit, dict) and isinstance(edit.get("file_path"), str):
            files.append(edit["file_path"])
    return files


def _tool_input_text(name: str, inp: Any) -> str:
    """A readable rendering of a tool call: the important argument first."""
    if not isinstance(inp, dict):
        return name
    lead_key = next((k for k in ("command", "file_path", "pattern", "query", "url", "prompt", "description")
                     if isinstance(inp.get(k), str)), None)
    if lead_key is None:
        return f"{name}: {json.dumps(inp, ensure_ascii=False, separators=(',', ':'))}"
    # The lead argument is shown once, as the heading; the JSON carries only the rest.
    rest = {k: v for k, v in inp.items() if k != lead_key}
    head = f"{name}: {inp[lead_key]}"
    return f"{head}\n{json.dumps(rest, ensure_ascii=False, separators=(',', ':'))}" if rest else head


def parse_record(rec: dict[str, Any], limits: Limits) -> RecordResult:
    out = RecordResult(session_id=rec.get("sessionId"))
    rtype = rec.get("type")

    if rtype == "ai-title" and rec.get("aiTitle"):
        out.title = str(rec["aiTitle"])
        return out
    if rtype == "custom-title" and rec.get("customTitle"):
        out.title, out.title_is_custom = str(rec["customTitle"]), True
        return out
    if rtype not in _CONTENT_TYPES:
        return out

    out.cwd = rec.get("cwd")
    branch = rec.get("gitBranch")
    out.git_branch = branch if branch and branch != "HEAD" else None  # "HEAD" = not a repo / detached
    out.entrypoint = rec.get("entrypoint")
    out.version = rec.get("version")
    uuid = rec.get("uuid") or rec.get("leafUuid")
    ts = rec.get("timestamp")
    side = bool(rec.get("isSidechain"))

    if rtype == "summary":
        if rec.get("summary") and uuid:
            out.messages.append(ParsedMessage(uuid, 0, ts, "summary", str(rec["summary"])))
        return out
    if not uuid or rec.get("isMeta"):
        # isMeta user records are injected skill bodies / caveats, not conversation.
        return out

    msg = rec.get("message")
    if not isinstance(msg, dict):
        return out
    content = msg.get("content")
    role = "summary" if rec.get("isCompactSummary") else rtype
    api_error = bool(rec.get("isApiErrorMessage"))

    if isinstance(content, str):
        text = _clean_user_text(content) if rtype == "user" else content.strip()
        if text:
            out.messages.append(ParsedMessage(uuid, 0, ts, role, _clip(text, limits.max_text_chars),
                                              is_sidechain=side, is_error=api_error))
        return out
    if not isinstance(content, list):
        return out

    for part, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            raw = block.get("text") or ""
            text = _clean_user_text(raw) if rtype == "user" else raw.strip()
            if text:
                out.messages.append(ParsedMessage(uuid, part, ts, role, _clip(text, limits.max_text_chars),
                                                  is_sidechain=side, is_error=api_error))
        elif btype == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                out.messages.append(ParsedMessage(uuid, part, ts, "thinking",
                                                  _clip(text, limits.max_text_chars), is_sidechain=side))
        elif btype == "tool_use":
            name = str(block.get("name") or "tool")
            inp = block.get("input")
            out.messages.append(ParsedMessage(
                uuid, part, ts, "tool_use",
                _clip(_tool_input_text(name, inp), limits.max_tool_input_chars),
                tool_name=name, is_sidechain=side, files=_tool_input_files(inp)))
        elif btype == "tool_result":
            text = _tool_result_text(block.get("content")).strip()
            if text:
                out.messages.append(ParsedMessage(
                    uuid, part, ts, "tool_result", _clip(text, limits.max_tool_result_chars),
                    is_sidechain=side, is_error=bool(block.get("is_error"))))
        # image / document / tool_reference blocks carry no indexable text.
    return out


def iter_complete_lines(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (end_offset, line) for each newline-terminated line.

    A trailing fragment without a newline is a record still being written by an
    active session; it is left for the next sync.
    """
    start = 0
    while True:
        nl = data.find(b"\n", start)
        if nl < 0:
            return
        yield nl + 1, data[start:nl]
        start = nl + 1


def parse_line(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("malformed JSON line")
    return rec if isinstance(rec, dict) else None
