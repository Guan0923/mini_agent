"""Sidebar metadata for user-visible threads."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SidebarThread:
    thread_id: str
    session_id: str
    title: str
    created_at: str
    updated_at: str
    last_activity_at: str
    archived_at: str | None = None
    deleted_at: str | None = None
    title_is_custom: bool = False

    @property
    def state(self) -> str:
        if self.deleted_at is not None:
            return "deleted"
        if self.archived_at is not None:
            return "archived"
        return "active"

    def to_dict(self) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "archived_at": self.archived_at,
            "deleted_at": self.deleted_at,
            "title_is_custom": self.title_is_custom,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> SidebarThread:
        return cls(
            thread_id=str(value.get("thread_id") or ""),
            session_id=str(value.get("session_id") or ""),
            title=str(value.get("title") or "新对话"),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or value.get("created_at") or ""),
            last_activity_at=str(value.get("last_activity_at") or value.get("created_at") or ""),
            archived_at=str(value["archived_at"]) if value.get("archived_at") is not None else None,
            deleted_at=str(value["deleted_at"]) if value.get("deleted_at") is not None else None,
            title_is_custom=bool(value.get("title_is_custom", False)),
        )


__all__ = ["SidebarThread"]
