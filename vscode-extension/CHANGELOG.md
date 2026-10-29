# Changelog

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
