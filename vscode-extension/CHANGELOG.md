# Changelog

## 0.2.0

- **Archive** sessions to hide them from search, the Sessions list, project counts and Claude's MCP searches. Unarchive any time; a "Show: Archived" view lists what's hidden, and **Filters → Include archived** searches it.
- **Delete from index** removes a session's messages and keeps it out of future syncs. **Health → Deleted from the index** lists deletions with **Restore**, which works while Claude Code still has the transcript.
- Bulk actions: tick sessions in the Sessions tab, then Archive, Unarchive or Delete them together.
- CLI: `cch archive`, `cch delete`, `cch deleted`, `cch restore`; `cch sessions --archived`.
- Claude Code's own transcript files are never modified or deleted by any of this.
- Fixed: the local server could reset the connection instead of returning an error when it rejected a request.
- The index database moves to schema v3 automatically. Older versions of the extension can't open an upgraded index, so update every editor where you use it.

## 0.1.2

- Reworded the Marketplace description: it now leads with what the extension does and ends with "Community extension, not affiliated with Anthropic."

## 0.1.1

Fixes found by running the test suite on Windows, Linux and macOS.

- **The MCP server now starts on Windows when your user folder contains a space** (for example `C:\Users\First Last`). Claude Code split the launch command at the space, so "Bring into this chat" and Claude's own history search couldn't work. Run **Claude History: Register MCP Server for Claude Code** again to apply the fix.
- The MCP server syncs the index before answering its first question, instead of sometimes searching a stale index right after it starts.
- Faster backend startup on macOS: it no longer does a slow network lookup when starting the local server.
- Project names are now correct when a transcript recorded a path in another operating system's format.

## 0.1.0

First public release.

- Global full-text search across every Claude Code session on the machine, with filters, context view, and session browser.
- Continue a past session in Claude Code's chat panel, bring it into a new chat in the current workspace, or resume it in the terminal.
- Context packs with provenance and secret redaction; send to chat, insert, copy or save.
- Command to register the claude-history MCP server so Claude can search your history from any repo.
- Local-only indexing that preserves history after Claude Code deletes old transcripts.
