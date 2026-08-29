"""Discover one bounded global or project ``AGENTS.md`` instruction source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_AGENT_INSTRUCTIONS_MAX_BYTES = 32 * 1024


class AgentInstructionsError(ValueError):
    """Raised when an instruction source cannot be loaded safely."""


@dataclass(frozen=True)
class AgentInstructionSource:
    """The single selected global or project instruction file."""

    path: Path
    display_path: str
    scope: Literal["global", "project"]
    content: str
    byte_count: int
    truncated: bool = False


@dataclass(frozen=True)
class AgentInstructions:
    """The optional instruction source included in one runner build."""

    source: AgentInstructionSource | None = None
    max_bytes: int = DEFAULT_AGENT_INSTRUCTIONS_MAX_BYTES

    @property
    def total_bytes(self) -> int:
        return self.source.byte_count if self.source is not None else 0

    @property
    def truncated(self) -> bool:
        return bool(self.source and self.source.truncated)

    def render(self) -> str:
        """Render the selected source with model-safe provenance."""

        if self.source is None:
            return ""
        truncation = " (truncated at the configured byte limit)" if self.source.truncated else ""
        return (
            f"### {self.source.scope.title()} instructions: {self.source.display_path}{truncation}\n"
            f"<agents-md>\n{self.source.content}\n</agents-md>"
        )


def discover_agent_instructions(
    *,
    global_root: Path | None,
    project_root: Path,
    max_bytes: int = DEFAULT_AGENT_INSTRUCTIONS_MAX_BYTES,
) -> AgentInstructions:
    """Select the project-root ``AGENTS.md``, falling back to the global file.

    Only ``<project_root>/AGENTS.md`` and ``<global_root>/AGENTS.md`` are
    considered. A non-empty project file replaces the global file entirely.
    Empty, whitespace-only, or symbolic-link files are ignored. The selected
    UTF-8 content is bounded by ``max_bytes``.
    """

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("AGENTS.md max_bytes must be a positive integer.")

    resolved_project_root = _resolve_directory(project_root, label="project root")
    project = _load_source(
        resolved_project_root / "AGENTS.md",
        scope="project",
        display_path="AGENTS.md",
        max_bytes=max_bytes,
    )
    if project is not None:
        return AgentInstructions(project, max_bytes)

    if global_root is None:
        return AgentInstructions(max_bytes=max_bytes)
    candidate = Path(global_root)
    if candidate.is_symlink() or not candidate.is_dir():
        return AgentInstructions(max_bytes=max_bytes)
    global_source = _load_source(
        candidate.resolve() / "AGENTS.md",
        scope="global",
        display_path="~/.mini_agent/AGENTS.md",
        max_bytes=max_bytes,
    )
    return AgentInstructions(global_source, max_bytes)


def _resolve_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AgentInstructionsError(f"AGENTS.md {label} is unavailable.") from exc
    if not resolved.is_dir():
        raise AgentInstructionsError(f"AGENTS.md {label} must be a directory.")
    return resolved


def _load_source(
    path: Path,
    *,
    scope: Literal["global", "project"],
    display_path: str,
    max_bytes: int,
) -> AgentInstructionSource | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
            file_size = handle.seek(0, 2)
    except OSError as exc:
        raise AgentInstructionsError(f"Unable to read AGENTS.md instruction source: {path}") from exc
    if not raw:
        return None

    included = raw[:max_bytes]
    source_truncated = file_size > len(included)
    content, byte_count = _decode_utf8_prefix(included, path, allow_trailing_partial=source_truncated)
    if not content.strip():
        return None
    return AgentInstructionSource(
        path=path.resolve(),
        display_path=display_path,
        scope=scope,
        content=content,
        byte_count=byte_count,
        truncated=source_truncated,
    )


def _decode_utf8_prefix(raw: bytes, path: Path, *, allow_trailing_partial: bool) -> tuple[str, int]:
    included = raw
    while included:
        try:
            return included.decode("utf-8"), len(included)
        except UnicodeDecodeError as exc:
            if not allow_trailing_partial or exc.reason != "unexpected end of data" or exc.end != len(included):
                raise AgentInstructionsError(f"AGENTS.md instruction source must be UTF-8: {path}") from exc
            included = included[: exc.start]
    return "", 0


__all__ = [
    "DEFAULT_AGENT_INSTRUCTIONS_MAX_BYTES",
    "AgentInstructionSource",
    "AgentInstructions",
    "AgentInstructionsError",
    "discover_agent_instructions",
]
