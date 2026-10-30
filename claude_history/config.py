"""Configuration loading.

Resolution order (later wins): built-in defaults -> config file -> environment.
The config file is TOML, looked up at $CLAUDE_HISTORY_CONFIG or
<data_dir>/config.toml. Nothing here is specific to one machine: the Claude
directory follows Claude Code's own CLAUDE_CONFIG_DIR convention.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _default_claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _default_data_dir() -> Path:
    env = os.environ.get("CLAUDE_HISTORY_DATA")
    return Path(env).expanduser() if env else Path.home() / ".claude-history"


@dataclass
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    # Directories containing Claude Code "projects/" trees. Usually one.
    claude_dirs: list[Path] = field(default_factory=lambda: [_default_claude_dir()])
    host: str = "127.0.0.1"
    port: int = 8765
    # Background poll interval for the web server's incremental sync (seconds).
    sync_interval: int = 20
    # MCP server re-syncs before answering if the last sync is older than this.
    mcp_sync_max_age: int = 30
    # Per-block truncation limits so huge tool outputs don't swamp the index.
    max_tool_result_chars: int = 6000
    max_tool_input_chars: int = 3000
    max_text_chars: int = 60000
    index_memory_files: bool = True
    redact_packs: bool = True
    # "Send whole session to chat": transcripts up to this size go into the chat
    # message itself; larger ones are saved to a file that Claude reads.
    whole_session_inline_tokens: int = 25000

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def packs_dir(self) -> Path:
        return self.data_dir / "packs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "claude-history.log"


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    candidate = path or os.environ.get("CLAUDE_HISTORY_CONFIG") or (cfg.data_dir / "config.toml")
    p = Path(candidate).expanduser()
    if p.is_file():
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
        for key, value in raw.items():
            if not hasattr(cfg, key):
                continue
            if key == "data_dir":
                value = Path(value).expanduser()
            elif key == "claude_dirs":
                value = [Path(v).expanduser() for v in value]
            setattr(cfg, key, value)
    if os.environ.get("CLAUDE_HISTORY_PORT"):
        cfg.port = int(os.environ["CLAUDE_HISTORY_PORT"])
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
