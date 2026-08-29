"""SQLite JSON-object persistence for right-panel state."""

from __future__ import annotations

from dataclasses import replace

from backend.domain.right_panel import RightPanelState, RightPanelWindow
from backend.domain.runtime_state import RuntimeState
from backend.domain.state import utc_now

_STATE_OBJECT_ID = "state"


class SQLiteRightPanelMixin:
    def get_right_panel_state(self, session_id: str) -> RightPanelState:
        with self._connection(session_id) as connection:
            value = self._json_object(connection, session_id, "right_panel_state", _STATE_OBJECT_ID)
        return RightPanelState.from_dict(value) if value is not None else RightPanelState(session_id)

    def save_right_panel_state(
        self,
        session_id: str,
        *,
        width: int | None = None,
        collapsed: bool | None = None,
        active_window_id: str | None | object = ...,
    ) -> RightPanelState:
        current = self.get_right_panel_state(session_id)
        changes: dict[str, object] = {}
        if width is not None:
            changes["width"] = width
        if collapsed is not None:
            changes["collapsed"] = collapsed
        if active_window_id is not ...:
            changes["active_window_id"] = active_window_id
        result = replace(current, **changes)
        now = utc_now()
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, session_id)
            self._put_json_object(
                connection,
                session_id,
                "right_panel_state",
                _STATE_OBJECT_ID,
                result.to_dict(),
                now,
            )
        return result

    def list_right_panel_windows(self, session_id: str, *, include_deleted: bool = False) -> list[RightPanelWindow]:
        with self._connection(session_id) as connection:
            values = self._json_values(connection, session_id, "right_panel_window")
        result = [RightPanelWindow.from_dict(value) for value in values]
        if not include_deleted:
            result = [item for item in result if item.active]
        return sorted(result, key=lambda item: (item.position, item.created_at, item.id))

    def get_right_panel_window(self, session_id: str, window_id: str) -> RightPanelWindow | None:
        with self._connection(session_id) as connection:
            value = self._json_object(connection, session_id, "right_panel_window", window_id)
        return RightPanelWindow.from_dict(value) if value is not None else None

    def active_right_panel_window_for_thread(self, session_id: str, thread_id: str) -> RightPanelWindow | None:
        return next(
            (
                item
                for item in self.list_right_panel_windows(session_id)
                if item.kind == "side_chat" and item.thread_id == thread_id
            ),
            None,
        )

    def create_right_panel_window(self, item: RightPanelWindow) -> RightPanelWindow:
        with self._connection(item.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, item.session_id)
            if self._json_object(connection, item.session_id, "right_panel_window", item.id) is not None:
                raise ValueError("Right-panel window already exists.")
            self._put_json_object(
                connection,
                item.session_id,
                "right_panel_window",
                item.id,
                item.to_dict(),
                item.updated_at,
            )
        return item

    def create_side_chat_window(self, item: RightPanelWindow, anchor: RuntimeState) -> RightPanelWindow:
        if item.kind != "side_chat" or item.thread_id != anchor.thread_id or item.anchor_turn_id != anchor.id:
            raise ValueError("Side-chat window and anchor identities do not match.")
        if item.session_id != anchor.session_id or anchor.status == "running":
            raise ValueError("Side-chat anchor must be a terminal Turn in the window Session.")
        with self._connection(item.session_id) as connection:
            self._assert_writable(connection)
            self._session_document(connection, item.session_id)
            if self._json_object(connection, item.session_id, "right_panel_window", item.id) is not None:
                raise ValueError("Right-panel window already exists.")
            if self._json_object(connection, item.session_id, "runtime_node", anchor.id) is not None:
                raise ValueError("Side-chat anchor already exists.")
            parent = self._json_object(connection, anchor.parent_session_id, "runtime_node", anchor.parent_id)
            if parent is None or anchor.parent_session_id != item.session_id:
                raise ValueError("Side-chat anchor parent is unavailable.")
            if parent.get("thread_id") != anchor.parent_thread_id:
                raise ValueError("Side-chat anchor parent Thread does not match.")
            self._ensure_runtime_thread_record(
                connection,
                session_id=anchor.session_id,
                thread_id=anchor.thread_id,
                origin_kind="fork",
                timestamp=anchor.timestamp,
            )
            self._put_json_object(
                connection,
                anchor.session_id,
                "runtime_node",
                anchor.id,
                anchor.to_dict(),
                anchor.timestamp,
            )
            self._set_thread_head(
                connection,
                session_id=anchor.session_id,
                thread_id=anchor.thread_id,
                turn_id=anchor.id,
                timestamp=anchor.timestamp,
                clear_running=True,
            )
            self._put_json_object(
                connection,
                item.session_id,
                "right_panel_window",
                item.id,
                item.to_dict(),
                item.updated_at,
            )
            self._touch_session(connection, item.session_id, item.updated_at)
        return item

    def update_right_panel_window(self, session_id: str, window_id: str, **changes: object) -> RightPanelWindow:
        current = self.get_right_panel_window(session_id, window_id)
        if current is None:
            raise KeyError(window_id)
        allowed = {"title", "deleted_at"}
        if set(changes) - allowed:
            raise ValueError("Unsupported right-panel window field.")
        if "title" in changes and not str(changes["title"]).strip():
            raise ValueError("Right-panel window title cannot be empty.")
        if "title" in changes:
            changes["title"] = str(changes["title"]).strip()
        result = replace(current, **changes, updated_at=utc_now())
        with self._connection(session_id) as connection:
            self._assert_writable(connection)
            self._put_json_object(
                connection,
                session_id,
                "right_panel_window",
                window_id,
                result.to_dict(),
                result.updated_at,
            )
        return result


__all__ = ["SQLiteRightPanelMixin"]
