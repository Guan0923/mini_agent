"""Run Mini-Agent (TUI client) from a source checkout.

Requires the workspace packages to be installed editable (e.g. ``uv sync`` or
``python -m pip install -e backend -e tui``); the ``backend`` and ``tui``
packages live in flat source directories (``backend/src``, ``tui/src``) and
are resolved through the installed package mapping.
"""

from tui.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
