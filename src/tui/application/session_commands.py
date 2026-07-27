"""Session-oriented slash-command behavior."""

from __future__ import annotations

import os


class SessionCommandMixin:
    def _ensure_session(self, title: str | None = None):
        if self.session_store is None:
            raise RuntimeError("Session storage is not configured.")
        created = self.active_session is None
        session = self._conversation_service.ensure_session(title)
        if created:
            self._print_active_session()
        return session

    def _new_session(self, title: str | None) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        self._clear_display()
        self._reset_context_usage()
        self._conversation_service.prepare_new_session(title)
        self.last_state = None
        self._print_active_session()

    def _resume_session(self, session_id: str | None) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        if getattr(self, "_view", None) is not None:
            self._pending_resume_id = session_id
            return
        try:
            self.resume_session(session_id)
        except (RuntimeError, ValueError) as exc:
            self._write(f"RESUME ERROR {exc}")
            return

    def _show_sessions(self) -> None:
        view = getattr(self, "_view", None)
        show_sessions = getattr(view, "show_sessions", None)
        if self.session_store is None:
            if callable(show_sessions):
                show_sessions(["Session storage is not configured."])
            else:
                self._write("Session storage is not configured.")
            return
        sessions = self._conversation_service.list_sessions()
        lines = [
            (
                f"{'*' if self.active_session and session.session_id == self.active_session.session_id else ' '} "
                f"{session.session_id} — {session.title} "
                f"({session.message_count} messages, updated {session.updated_at})"
            )
            for session in sessions
        ]
        if callable(show_sessions):
            show_sessions(lines)
            return
        if not sessions:
            self._write("No saved sessions.")
            return
        for line in lines:
            self._write(line)

    def _show_session(self) -> None:
        if self.session_store is None:
            self._write("Session storage is not configured.")
            return
        pending_title = self._conversation_service.pending_session_title
        if pending_title is not None:
            self._write("SESSION PENDING")
            self._write(f"TITLE {pending_title}")
            self._write("MESSAGES 0")
            self._write("STATUS Not saved yet")
            return
        if self.active_session is None:
            self._write("No active session.")
            return
        summary = self._conversation_service.current_summary()
        if summary is None:
            self._write("No active session.")
            return
        self._load_active_history()
        self._write(f"SESSION {summary.session_id}")
        self._write(f"TITLE {summary.title}")
        self._write(f"MESSAGES {summary.message_count}")
        self._write(f"CREATED {summary.created_at}")
        self._write(f"UPDATED {summary.updated_at}")
        if summary.last_run_id:
            self._write(f"LAST RUN {summary.last_run_id} {summary.last_run_status}")

    def _load_active_history(self) -> None:
        """Replace the main transcript with the active session's persisted history."""

        view = getattr(self, "_view", None)
        load_history = getattr(view, "load_history", None)
        if self.active_session is None or not callable(load_history):
            return
        history_page = getattr(self._conversation_service, "history_page", None)
        messages, _ = (
            history_page(limit=50) if callable(history_page) else (self._conversation_service.history()[-50:], None)
        )
        load_history(messages)

    def _show_history(self) -> None:
        view = getattr(self, "_view", None)
        show_history = getattr(view, "show_history", None)
        if self.session_store is None:
            if callable(show_history):
                show_history("No session storage", [])
            else:
                self._write("Session storage is not configured.")
            return
        if self.active_session is None:
            if callable(show_history):
                pending = self._conversation_service.pending_session_title
                show_history(f"Pending: {pending}" if pending else "No active session", [])
            else:
                self._write(
                    "No conversation history."
                    if self._conversation_service.pending_session_title is not None
                    else "No active session."
                )
            return
        history_page = getattr(self._conversation_service, "history_page", None)
        messages, before_id = (
            history_page(limit=100) if callable(history_page) else (self._conversation_service.history(), None)
        )
        if callable(show_history):
            session = self.active_session
            try:
                show_history(
                    f"{session.session_id} — {session.title}",
                    messages,
                    before_id=before_id,
                    load_older=(
                        (lambda cursor: history_page(before_id=cursor, limit=100)) if callable(history_page) else None
                    ),
                )
            except TypeError:
                show_history(f"{session.session_id} — {session.title}", messages)
            return
        if not messages:
            self._write("No conversation history.")
            return
        self._write(f"HISTORY {self.active_session.session_id}")
        for message in messages:
            role = message["role"].upper()
            self._write(f"{role}\n{message['content']}")

    def _show_fork_selector(self) -> None:
        view = getattr(self, "_view", None)
        begin_review = getattr(view, "begin_review", None)
        list_runs = getattr(self.session_store, "list_forkable_runs", None)
        if not callable(begin_review):
            self._write("Usage: /fork <run_id>")
            return
        runs = list_runs() if callable(list_runs) else []
        if not runs:
            self._write("No finished runs are available to fork.")
            return
        from ..widgets import ChoiceItem

        begin_review(
            "FORK RUN",
            "Choose a finished run",
            "A new session ID will be created; resume it in another terminal.",
            tuple(
                ChoiceItem(item["run_id"], item["run_id"], f"{item['status']} — {item['task'][:80]}")
                for item in runs[:50]
            ),
            lambda choice, _supplement: self._fork_run(choice),
        )

    def _fork_run(self, run_id: str) -> None:
        if not run_id:
            self._show_fork_selector()
            return
        fork = getattr(self.session_store, "fork_run", None)
        if not callable(fork):
            self._write("Fork is not supported by this session store.")
            return
        try:
            session = fork(run_id)
        except ValueError as exc:
            self._write(f"FORK ERROR {exc}")
            return
        self._write(f"FORKED SESSION {session.session_id}")
        self._write(f"Resume in another terminal: /resume {session.session_id}")

    def _print_active_session(self) -> None:
        if self.active_session is not None:
            self._write(f"SESSION {self.active_session.session_id} — {self.active_session.title}")
            return
        pending_title = self._conversation_service.pending_session_title
        if pending_title is not None:
            self._write(f"SESSION PENDING — {pending_title} (not saved yet)")

    @staticmethod
    def _clear_terminal() -> None:
        os.system("cls" if os.name == "nt" else "clear")
