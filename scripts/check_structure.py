"""Enforce production source size and package fan-out limits.

The check intentionally ignores tests and generated/dependency directories. It
is small enough to run in local development and in CI without importing the
application, so it remains useful while packages are being reorganized.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (ROOT / "backend" / "src", ROOT / "frontend" / "src")
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".css"}
IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
MAX_LINES = 500
MAX_DIRECT_FILES = 10


def _is_ignored(path: Path) -> bool:
    return any(part.lower() in IGNORED_DIRECTORY_NAMES for part in path.parts)


def _is_production_source(path: Path) -> bool:
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    lowered = path.name.lower()
    if any(part.lower() in {"test", "tests"} for part in path.parent.parts):
        return False
    if lowered.startswith("test_") or lowered.endswith("_test.py"):
        return False
    return not any(token in lowered for token in (".test.", ".spec.", "test-setup."))


def _source_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        path for path in root.rglob("*") if path.is_file() and not _is_ignored(path) and _is_production_source(path)
    ]


def _display(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def main() -> int:
    files_by_root: dict[Path, list[Path]] = {root: _source_files(root) for root in SOURCE_ROOTS}
    violations: list[str] = []

    for files in files_by_root.values():
        for path in files:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            if line_count > MAX_LINES:
                violations.append(f"file {_display(path)} has {line_count} lines (limit {MAX_LINES})")

    for root, files in files_by_root.items():
        directories = {root, *(path.parent for path in files)}
        for directory in sorted(directories):
            direct_files = [path for path in files if path.parent == directory]
            if len(direct_files) > MAX_DIRECT_FILES:
                violations.append(
                    f"package {_display(directory)} has {len(direct_files)} direct production files "
                    f"(limit {MAX_DIRECT_FILES})"
                )

    if violations:
        print("Structure check failed:")
        print("\n".join(f"- {item}" for item in violations))
        return 1

    total = sum(len(files) for files in files_by_root.values())
    print(f"Structure check passed: {total} production source files satisfy the limits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
