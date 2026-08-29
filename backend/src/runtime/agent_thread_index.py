"""Process-local indexes rebuilt from the canonical SQLite Thread tables."""

from __future__ import annotations

from threading import RLock

from backend.domain import RuntimeThread, ThreadNode


class AgentThreadIndex:
    """Lock-protected lookup cache; SQLite remains the only authority."""

    def __init__(self) -> None:
        self.session_threads: dict[str, set[str]] = {}
        self.thread_sessions: dict[str, str] = {}
        self.thread_heads: dict[str, str | None] = {}
        self.path_threads: dict[tuple[str, str, str], str] = {}
        self.thread_paths: dict[str, str] = {}
        self.thread_roots: dict[str, str] = {}
        self._lock = RLock()

    def rebuild(self, store: object) -> None:
        sessions = getattr(store, "list_sessions")(state="all")
        runtime_threads: list[RuntimeThread] = []
        thread_nodes: list[ThreadNode] = []
        for session in sessions:
            runtime_threads.extend(getattr(store, "list_runtime_threads")(session.session_id))
            thread_nodes.extend(getattr(store, "list_thread_nodes")(session.session_id))
        with self._lock:
            self._replace(runtime_threads, thread_nodes)

    def refresh_session(self, store: object, session_id: str) -> None:
        runtime_threads = getattr(store, "list_runtime_threads")(session_id)
        thread_nodes = getattr(store, "list_thread_nodes")(session_id)
        with self._lock:
            stale = self.session_threads.pop(session_id, set())
            for thread_id in stale:
                self.thread_sessions.pop(thread_id, None)
                self.thread_heads.pop(thread_id, None)
                path = self.thread_paths.pop(thread_id, None)
                root_thread_id = self.thread_roots.pop(thread_id, None)
                if path is not None and root_thread_id is not None:
                    self.path_threads.pop((session_id, root_thread_id, path), None)
            self._add(runtime_threads, thread_nodes)

    def _replace(self, runtime_threads: list[RuntimeThread], thread_nodes: list[ThreadNode]) -> None:
        self.session_threads.clear()
        self.thread_sessions.clear()
        self.thread_heads.clear()
        self.path_threads.clear()
        self.thread_paths.clear()
        self.thread_roots.clear()
        self._add(runtime_threads, thread_nodes)

    def _add(self, runtime_threads: list[RuntimeThread], thread_nodes: list[ThreadNode]) -> None:
        for thread in runtime_threads:
            self.session_threads.setdefault(thread.session_id, set()).add(thread.thread_id)
            self.thread_sessions[thread.thread_id] = thread.session_id
            self.thread_heads[thread.thread_id] = thread.current_turn_id
        for node in thread_nodes:
            self.path_threads[(node.session_id, node.root_thread_id, node.thread_path)] = node.thread_id
            self.thread_paths[node.thread_id] = node.thread_path
            self.thread_roots[node.thread_id] = node.root_thread_id

    def threads_for_session(self, session_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(self.session_threads.get(session_id, ()))

    def session_for_thread(self, thread_id: str) -> str | None:
        with self._lock:
            return self.thread_sessions.get(thread_id)

    def head_for_thread(self, thread_id: str) -> str | None:
        with self._lock:
            return self.thread_heads.get(thread_id)

    def thread_for_path(self, session_id: str, root_thread_id: str, thread_path: str) -> str | None:
        with self._lock:
            return self.path_threads.get((session_id, root_thread_id, thread_path))

    def path_for_thread(self, thread_id: str) -> str | None:
        with self._lock:
            return self.thread_paths.get(thread_id)


__all__ = ["AgentThreadIndex"]
