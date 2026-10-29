# Claude Code history: what is on disk, and its limits

These findings come from inspecting a real Windows 11 installation (Claude Code 2.1.x, used from the VS Code extension and the desktop app) and validating the parser against every local transcript there. The parser ignores record and block types it doesn't know, so new fields in future versions won't break it.

## Locations

| Path | Contents | Used |
|---|---|---|
| `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` | One transcript per session (append-only JSONL) | yes |
| `~/.claude/projects/<encoded-cwd>/<sessionId>/subagents/agent-*.jsonl` | Subagent (sidechain) transcripts, same `sessionId` as the parent | yes |
| `~/.claude/projects/<encoded-cwd>/memory/*.md` | Per-project memory files | yes |
| `~/.claude/projects/<encoded-cwd>/<sessionId>/tool-results/` | Large tool outputs spilled to disk | no (binary/large; summary text is already in the transcript) |
| `~/.claude/sessions/<pid>.json` | Live-process registry (pid, cwd, sessionId) | no (transient) |
| `~/.claude/file-history/` | File snapshots for checkpoint/rewind | no |
| `~/.claude.json` | Global config incl. user-scope `mcpServers` | written once by `cch install-mcp` |

`CLAUDE_CONFIG_DIR` relocates `~/.claude`; the app follows it.

**Project association.** The directory name is the session's working directory with every non-alphanumeric character replaced by `-` (`D:\Work\My App` → `d--Work-My-App`, and the drive letter's case varies). This is lossy, so the app takes the real path from each record's `cwd` field. For projects whose transcripts are gone, it rebuilds the path by matching the encoded name against real folders on disk. Claude Code's `/resume` picker and the VS Code history list are filtered by this directory, which is why history appears to be "per repo". The data itself is global.

## Record types (one JSON object per line)

| `type` | Meaning | Indexed as |
|---|---|---|
| `user` | Human turn, or tool results (`content[].type == "tool_result"`) | `user` / `tool_result` |
| `assistant` | Model turn: `text`, `thinking`, `tool_use` blocks; one block per line, sharing `message.id` | `assistant` / `thinking` / `tool_use` |
| `summary` / `isCompactSummary` | Compaction summaries | `summary` |
| `ai-title`, `custom-title` | Session titles (custom wins) | session title |
| `attachment`, `api-request*`, `queue-operation`, `mode`, `last-prompt`, `file-history-*`, `atis-latch`, `cost-state`, `tools-used`, `agent-name`, `system` | Bookkeeping, environment and request blobs | skipped |

Useful fields on `user`/`assistant`: `uuid`, `parentUuid`, `timestamp` (ISO UTC), `sessionId`, `cwd`, `gitBranch` (`HEAD` when not a repo), `entrypoint` (`claude-vscode`, `claude-desktop`…), `version`, `isSidechain`, `isMeta` (injected skill text, skipped), `isApiErrorMessage`.

Text cleanup: `<system-reminder>` blocks and `<pasted_content>` wrapper tags are stripped from user text. Tool inputs are rendered as `Tool: main-argument + JSON`. File paths come from `file_path` / `path` / `notebook_path` tool arguments.

## How ingestion stays safe

- Files are opened read-only. The acceptance run hashes every file before and after.
- Sync is incremental by byte offset. Each file is committed in a single transaction together with its new offset, so a crash mid-file rolls back cleanly and the next sync resumes.
- A trailing line without a newline is a record that is still being written. It is left for the next sync.
- A file that shrank, or whose already-indexed prefix changed, is reindexed from scratch.
- Messages are unique on `(sessionId, record uuid, block index)`.
- Malformed lines are counted and skipped (see the Index health tab).
- When a source disappears it is marked `missing`, and its indexed messages are kept.

## Limitations

1. **Claude Code deletes old transcripts.** With no `cleanupPeriodDays` in `~/.claude/settings.json`, the default is about 30 days. Anything deleted before the first sync can't be recovered. From now on the index keeps every message it has seen, even after the source is deleted. The MCP server syncs at the start of every Claude Code session, and the web app syncs every 20 s. To keep the originals as well, raise `cleanupPeriodDays`.
2. **Thinking** is often stored empty or redacted (only signatures). It's indexed when the text is present, and hidden in the UI by default.
3. **Large tool outputs** are truncated in the index (6,000 characters by default). Image and PDF blocks aren't indexed.
4. **Resumed or forked sessions** may repeat earlier messages under a new session id. These are kept per session, faithful to the source, so a search can show the same text twice from two sessions.
5. **Session titles** come from `ai-title`/`custom-title`. Older sessions without one fall back to the first prompt.
6. **Resuming** needs Claude Code's own transcript and the original directory (`claude --resume` looks the session up by directory). The VS Code extension has no documented way for an outside program to open a specific session or type into its integrated terminal. So **Continue in VS Code** opens the folder and copies `cch resume <id>` for you to paste, and **Resume in terminal** uses a separate console. `cch resume` finds the newest `claude` binary bundled with the editor extension.
7. Full-text search uses SQLite's `unicode61` tokenizer, so `load_workbook` and `foo.bar()` match as phrases of their parts. For arbitrary fragments, use **Literal text** mode (`--literal` in the CLI).
