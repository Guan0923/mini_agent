"""Persist user-visible SidebarThread metadata separately from runtime threads."""

from __future__ import annotations

from dataclasses import replace

from backend.domain.sidebar_thread import SidebarThread
from backend.domain.state import utc_now


class SQLiteSidebarThreadMixin:
    def create_sidebar_thread(
        self,
        *,
        session_id: str,
        thread_id: str,
        title: str,
        title_is_custom: bool = False,
    ) -> SidebarThread:
        now = utc_now()
        item = SidebarThread(
            thread_id,
            session_id,
            title.strip() or "新对话",
            now,
            now,
            now,
            title_is_custom=title_is_custom,
        )
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            if self._json_object(connection, session_id, "sidebar_thread", thread_id) is not None:
                raise ValueError("SidebarThread already exists.")
            self._put_json_object(connection, session_id, "sidebar_thread", thread_id, item.to_dict(), now)
        return item

    def get_sidebar_thread(self, thread_id: str) -> SidebarThread | None:
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                value = self._json_object(connection, summary.session_id, "sidebar_thread", thread_id)
            if value is not None:
                return SidebarThread.from_dict(value)
        return None

    def list_sidebar_threads(self, *, state: str = "active") -> list[SidebarThread]:
        result: list[SidebarThread] = []
        for summary in self.list_sessions(state="all"):
            with self._connection(summary.session_id) as connection:
                result.extend(
                    SidebarThread.from_dict(value)
                    for value in self._json_values(connection, summary.session_id, "sidebar_thread")
                )
        if state != "all":
            result = [item for item in result if item.state == state]
        return sorted(result, key=lambda item: (item.updated_at, item.thread_id), reverse=True)

    def update_sidebar_thread(self, thread_id: str, **changes: object) -> SidebarThread:
        item = self.get_sidebar_thread(thread_id)
        if item is None:
            raise KeyError(thread_id)
        allowed = {"title", "archived_at", "deleted_at", "title_is_custom"}
        if set(changes) - allowed:
            raise ValueError("Unsupported SidebarThread field.")
        if "title" in changes and not str(changes["title"]).strip():
            raise ValueError("SidebarThread title cannot be empty.")
        next_item = replace(item, **changes, updated_at=utc_now())
        with self._connection(item.session_id) as connection:
            self._assert_writable(connection)
            self._put_json_object(
                connection, item.session_id, "sidebar_thread", item.thread_id, next_item.to_dict(), next_item.updated_at
            )
        return next_item

    def touch_sidebar_thread_activity(self, thread_id: str, *, timestamp: str | None = None) -> SidebarThread:
        item = self.get_sidebar_thread(thread_id)
        if item is None:
            raise KeyError(thread_id)
        activity_at = timestamp or utc_now()
        next_item = replace(item, last_activity_at=activity_at)
        with self._connection(item.session_id) as connection:
            self._assert_writable(connection)
            self._put_json_object(
                connection,
                item.session_id,
                "sidebar_thread",
                item.thread_id,
                next_item.to_dict(),
                activity_at,
            )
        return next_item


__all__ = ["SQLiteSidebarThreadMixin"]
