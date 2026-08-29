"""Persistent right-panel layout and window metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RightPanelWindowKind = Literal["side_chat", "terminal"]


@dataclass(frozen=True, slots=True)
class RightPanelState:
    session_id: str
    width: int = 420
    collapsed: bool = True
    active_window_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("RightPanelState session_id is required.")
        if isinstance(self.width, bool) or not 0 <= self.width <= 100_000:
            raise ValueError("RightPanelState width must be a non-negative integer.")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "width": self.width,
            "collapsed": self.collapsed,
            "active_window_id": self.active_window_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RightPanelState:
        active = value.get("active_window_id")
        return cls(
            session_id=str(value.get("session_id") or ""),
            width=int(value.get("width", 420)),
            collapsed=bool(value.get("collapsed", True)),
            active_window_id=str(active) if active is not None else None,
        )


@dataclass(frozen=True, slots=True)
class RightPanelWindow:
    id: str
    session_id: str
    kind: RightPanelWindowKind
    title: str
    position: int
    created_at: str
    updated_at: str
    thread_id: str | None = None
    anchor_turn_id: str | None = None
    terminal_id: str | None = None
    terminal_type: str | None = None
    cwd: str | None = None
    deleted_at: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.session_id or not self.title:
            raise ValueError("RightPanelWindow identifiers and title are required.")
        if self.kind not in {"side_chat", "terminal"}:
            raise ValueError("RightPanelWindow kind must be side_chat or terminal.")
        if isinstance(self.position, bool) or self.position < 0:
            raise ValueError("RightPanelWindow position must be non-negative.")
        if self.kind == "side_chat" and (not self.thread_id or not self.anchor_turn_id or self.terminal_id):
            raise ValueError("A side-chat window requires thread_id and anchor_turn_id only.")
        if self.kind == "terminal" and (
            not self.terminal_id or not self.terminal_type or not self.cwd or self.thread_id or self.anchor_turn_id
        ):
            raise ValueError("A terminal window requires terminal metadata only.")

    @property
    def active(self) -> bool:
        return self.deleted_at is None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "kind": self.kind,
            "title": self.title,
            "position": self.position,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "thread_id": self.thread_id,
            "anchor_turn_id": self.anchor_turn_id,
            "terminal_id": self.terminal_id,
            "terminal_type": self.terminal_type,
            "cwd": self.cwd,
            "deleted_at": self.deleted_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RightPanelWindow:
        def optional(name: str) -> str | None:
            item = value.get(name)
            return str(item) if item is not None else None

        return cls(
            id=str(value.get("id") or ""),
            session_id=str(value.get("session_id") or ""),
            kind=str(value.get("kind") or ""),  # type: ignore[arg-type]
            title=str(value.get("title") or ""),
            position=int(value.get("position", 0)),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or value.get("created_at") or ""),
            thread_id=optional("thread_id"),
            anchor_turn_id=optional("anchor_turn_id"),
            terminal_id=optional("terminal_id"),
            terminal_type=optional("terminal_type"),
            cwd=optional("cwd"),
            deleted_at=optional("deleted_at"),
        )


__all__ = ["RightPanelState", "RightPanelWindow", "RightPanelWindowKind"]
