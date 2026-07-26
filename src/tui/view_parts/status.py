"""Status-line rendering and owner-thread callback dispatch."""

from __future__ import annotations

from collections.abc import Callable
from threading import get_ident

from textual._context import active_app

COPY_NOTICE_SECONDS = 1.5
RUNNING_STATUS_MIN_SECONDS = 5.0
RUNNING_STATUS_MAX_SECONDS = 10.0

RUNNING_STATUS_WORDS = (
    "🧠 THINKING",
    "🧪 BREWING",
    "🧭 EXPLORING",
    "🔌 CONNECTING",
    "🧵 WEAVING",
    "🔎 SCOUTING",
    "🧩 ASSEMBLING",
    "✨ POLISHING",
    "📝 DRAFTING",
    "📚 READING",
    "🗺️ MAPPING",
    "🛠️ BUILDING",
    "⚙️ TUNING",
    "📐 STRUCTURING",
    "🔬 ANALYZING",
    "💡 SPARKING",
    "🌱 GROWING",
    "🚀 LAUNCHING",
    "🛰️ SCANNING",
    "🧮 COMPUTING",
    "🧹 SORTING",
    "🧱 STACKING",
    "🎯 FOCUSING",
    "🛡️ VALIDATING",
    "🔐 CHECKING",
    "📦 PACKING",
    "🔄 REFINING",
    "🧵 THREADING",
    "🧠 REASONING",
    "🗣️ FORMULATING",
    "📡 SIGNALING",
    "🧰 TOOLING",
    "🌊 FLOWING",
    "🎨 SHAPING",
    "✅ VERIFYING",
    "🏁 FINISHING",
    "🏃 RUNNING",
)


class ViewStatusMixin:
    def _refresh_status(self) -> None:
        self._invalidate_copy_notice()
        self._render_status()

    @staticmethod
    def _is_running_status(status: str) -> bool:
        return any(part.strip() == "RUNNING" for part in status.split("|"))

    def _schedule_running_status(self) -> None:
        if self._writes_closed or not self.is_running or not self._is_running_status(self._status):
            return
        delay = self._status_random.uniform(
            RUNNING_STATUS_MIN_SECONDS,
            RUNNING_STATUS_MAX_SECONDS,
        )
        self._status_timer = self.set_timer(delay, self._rotate_running_status)

    def _rotate_running_status(self) -> None:
        self._status_timer = None
        if not self._is_running_status(self._status):
            self._running_status = None
            return
        self._choose_running_status()
        if self._copy_notice_timer is None:
            self._render_status()
        self._schedule_running_status()

    def _choose_running_status(self) -> None:
        choices = tuple(word for word in RUNNING_STATUS_WORDS if word != self._running_status)
        self._running_status = self._status_random.choice(choices)

    def _stop_running_status(self) -> None:
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer = None
        self._running_status = None

    def _render_status(self) -> None:
        suffix = " | PgUp/PgDn scroll" if not self._follow_tail else ""
        status = self._status
        if self._running_status is not None:
            status = status.replace(" | RUNNING", f" | {self._running_status}", 1)
        self.status_line.update(f" {status}{suffix}")

    def _run_on_owner(
        self,
        callback: Callable[[], None],
        *,
        diagnostic_name: str = "owner_callback",
        diagnostic_data: dict[str, object] | None = None,
    ) -> None:
        def guarded() -> None:
            try:
                callback()
            except Exception as error:
                self._diagnose(
                    "owner_callback_failed",
                    {
                        "callback": diagnostic_name,
                        **dict(diagnostic_data or {}),
                        **self.diagnostic_snapshot(),
                    },
                    error,
                )
                raise

        if not self._owner_ready:
            self._pending_owner_callbacks.append(guarded)
            return
        try:
            if active_app.get() is self and self._thread_id == get_ident():
                guarded()
                return
        except LookupError:
            pass
        self.call_later(guarded)
