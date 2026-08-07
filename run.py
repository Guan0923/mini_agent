"""Run Mini-Agent (TUI client) directly from a source checkout."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "tui" / "src"))


from tui.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
