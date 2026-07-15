"""Run Mini-Agent directly from a source checkout."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mini_agent.tui.cli import main


if __name__ == "__main__":
    main()
