"""Persist user-visible SidebarThread metadata separately from runtime threads."""

from __future__ import annotations

from dataclasses import replace

from backend.domain.runtime_state import RuntimeState, RuntimeStateTree
from backend.domain.sidebar_thread import SidebarThread, SidebarThreadSummary
from backend.domain.state import utc_now


class SQLiteSidebarThreadMixin:
    def _sidebar_summaries_for_session(self, session_id: str) -> list[SidebarThreadSummary]:
        with self._connection(session_id) as connection:
            threads = [
                SidebarThread.from_dict(value) for value in self._json_values(connection, session_id, "sidebar_thread")
            ]
            nodes = self._objects(connection, session_id, "runtime_node")
            runtime_rows = connection.execute(
                "SELECT thread_id,current_turn_id,updated_at FROM runtime_threads WHERE session_id=?",
                (session_id,),
            ).fetchall()

        tree = RuntimeStateTree(nodes)
        runtime_by_thread = {str(row["thread_id"]): row for row in runtime_rows}
        summaries: list[SidebarThreadSummary] = []
        for thread in threads:
            runtime = runtime_by_thread.get(thread.thread_id)
            current_turn_id = str(runtime["current_turn_id"]) if runtime and runtime["current_turn_id"] else None
            path = tree.ancestors((session_id, current_turn_id)) if current_turn_id else []
            records = self._node_records([node for node in path if isinstance(node, RuntimeState)])
            summaries.append(
                SidebarThreadSummary(
                    thread=thread,
                    message_count=len(records),
                    conversation_updated_at=(
                        str(runtime["updated_at"]) if current_turn_id and runtime else thread.created_at
                    ),
                )
            )
        return summaries

    def sidebar_thread_summary(self, item: SidebarThread) -> SidebarThreadSummary:
        return next(
            summary
            for summary in self._sidebar_summaries_for_session(item.session_id)
            if summary.thread.thread_id == item.thread_id
        )

    def list_sidebar_thread_summaries(self, *, state: str = "active") -> list[SidebarThreadSummary]:
        if state not in {"active", "archived", "deleted", "all"}:
            raise ValueError(f"Unknown SidebarThread state: {state}")
        result: list[SidebarThreadSummary] = []
        for directory in self.paths.runtime_dir.iterdir():
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or (directory / "state.db").is_symlink()
                or not (directory / "state.db").is_file()
            ):
                continue
            result.extend(self._sidebar_summaries_for_session(directory.name))
        if state != "all":
            result = [item for item in result if item.thread.state == state]
        return sorted(
            result,
            key=lambda item: (item.conversation_updated_at, item.thread.thread_id),
            reverse=True,
        )

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
            thread_id, session_id, title.strip() or "新对话", now, now, title_is_custom=title_is_custom
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


__all__ = ["SQLiteSidebarThreadMixin"]
