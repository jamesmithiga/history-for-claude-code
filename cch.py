"""Entry point that works from any directory: python path/to/cch.py <command>."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_history.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
