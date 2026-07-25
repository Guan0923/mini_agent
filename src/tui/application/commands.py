"""Slash-command parsing and session presentation."""

from __future__ import annotations

import json
import os
import re

from backend.domain import PlanningError

from ..components.commands import COMMAND_ARGUMENT_NAMES, COMMAND_PATTERN, render_help
from ..rendering.state import DETAIL_LEVELS
from ..widgets import ChoiceItem

HELP = render_help()


class CommandAppMixin:
    def _handle(self, task: str) -> bool:
        if not task:
            return True
        parts = self._split_input(task)
        for standalone in ("history", "compact"):
            has_command = any(kind == "command" and value == standalone for kind, value, _argument in parts)
            if has_command and task.strip() != f"/{standalone}":
                self._write(f"Usage: /{standalone}")
                return True
        for kind, value, argument in parts:
            if kind == "task":
                self.run_task(value)
                continue
            if not self._handle_command(value, argument):
                return False
        return True

    @staticmethod
    def _split_input(value: str) -> list[tuple[str, str, str]]:
        """Run recognized commands first, then return at most one merged task."""

        if re.search(r"(?<!\S)/(?:use|session)(?=\s|$)", value):
            return [("command", "legacy_session", "")]

        matches = list(COMMAND_PATTERN.finditer(value))
        if not matches:
            return [("task", value, "")]

        commands: list[tuple[str, str, str]] = []
        task_parts = [value[: matches[0].start()]]
        for index, match in enumerate(matches):
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            command = match.group("name")
            following_text = value[match.end() : next_start]
            if command == "quit":
                commands.append(("command", command, ""))
                return commands
            if command in COMMAND_ARGUMENT_NAMES:
                commands.append(("command", command, following_text.strip()))
            else:
                commands.append(("command", command, ""))
                task_parts.append(following_text)

        merged_task = " ".join(part.strip() for part in task_parts if part.strip())
        if merged_task:
            commands.append(("task", merged_task, ""))
        return commands

    def _handle_command(self, command: str, argument: str) -> bool:
        if command == "agent":
            if argument:
                self._write("Usage: /agent")
                return True
            self.mode = "agent"
            self._write("Agent mode enabled.")
            return True
        if command == "plan":
            if argument:
                self._write("Usage: /plan")
                return True
            self.mode = "plan"
            self._write(
                "Plan mode enabled: read-only discussion with Plan Review available when needed. Use /agent to return to Agent mode."
            )
            return True
        if command == "permission":
            if argument:
                self._write("Usage: /permission")
                return True
            self._approval.configure_permission()
            return True
        if command == "display":
            if not argument:
                self._show_display_selector()
                return True
            detail_level = argument.casefold()
            if detail_level not in DETAIL_LEVELS:
                self._write("Usage: /display <minimal|medium|verbose>")
                return True
            self._set_display_mode(detail_level)
            return True
        if command == "sessions":
            if argument:
                self._write("Usage: /sessions")
                return True
            self._show_sessions()
            return True
        if command == "resume":
            self._resume_session(argument or None)
            return True
        if command == "history":
            if argument:
                self._write("Usage: /history")
                return True
            self._show_history()
            return True
        if command in {"new", "clear"}:
            self._new_session(argument or None)
            return True
        if command == "legacy_session":
            self._write("/use and /session were removed; use /resume [session_id].")
            return True
        if command == "quit":
            if argument:
                self._write("Usage: /quit")
                return True
            self._write("Bye.")
            return False
        if command == "help":
            if argument:
                self._write("Usage: /help")
                return True
            self._write(HELP)
            return True
        if command == "tools":
            if argument:
                self._write("Usage: /tools")
                return True
            self._write("\n".join(self.runner.tools.names()))
            return True
        if command == "skills":
            if argument:
                self._write("Usage: /skills")
                return True
            catalog = getattr(self.runner, "skill_catalog", None)
            definitions = catalog.definitions() if catalog is not None else ()
            if not definitions:
                self._write("No project Skills found in .mini_agent/skills.")
                return True
            self._write("\n".join(f"{skill.name} — {skill.description}" for skill in definitions))
            return True
        if command == "compact":
            if argument:
                self._write("Usage: /compact")
                return True
            try:
                result = self._conversation_service.compact_context()
            except (PlanningError, RuntimeError) as exc:
                self._write(f"COMPACT ERROR {exc}")
                return True
            if result.compacted:
                self._write(f"COMPACTED {result.previous_messages} → {result.remaining_messages} messages")
            else:
                self._write("No old conversation context to compact.")
            return True
        if command == "trace":
            if argument:
                self._write("Usage: /trace")
                return True
            self._show_trace()
            return True
        return True

    def _show_display_selector(self) -> None:
        view = getattr(self, "_view", None)
        begin_review = getattr(view, "begin_review", None)
        if not callable(begin_review):
            self._write("Display selector requires the interactive TUI.")
            return
        begin_review(
            "DISPLAY MODE",
            f"Current: {self._display_mode.title()}",
            "Choose how much detail future agent runs show.",
            (
                ChoiceItem("minimal", "Minimal", "Processing status and final responses only."),
                ChoiceItem("medium", "Medium", "Thinking and tool names, without tool details."),
                ChoiceItem("verbose", "Verbose", "Thinking, responses, and expandable tool details."),
            ),
            lambda choice, _supplement: self._set_display_mode(choice),
            initial_choice_id=self._display_mode,
        )

    def _set_display_mode(self, detail_level: str) -> None:
        self._display_mode = detail_level
        view = getattr(self, "_view", None)
        set_detail_level = getattr(view, "set_detail_level", None)
        if callable(set_detail_level):
            set_detail_level(detail_level)
        set_ui = getattr(view, "set_ui", None)
        if callable(set_ui):
            set_ui(status=self._status_with_permission("AGENT | IDLE"), interrupt_enabled=False)
        self._write(f"Display mode set to {detail_level}.")

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

    def _show_trace(self) -> None:
        view = getattr(self, "_view", None)
        show_trace = getattr(view, "show_trace", None)
        trace = (
            json.dumps(self.last_state.to_dict(), ensure_ascii=False, indent=2) if self.last_state else "No run yet."
        )
        if callable(show_trace):
            show_trace(self.last_state.run_id if self.last_state else "No run yet", trace)
            return
        self._write(trace)

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
