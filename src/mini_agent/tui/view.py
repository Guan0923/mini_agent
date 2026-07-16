"""Full-screen prompt-toolkit view with a stable bottom input row."""

from __future__ import annotations

import asyncio
from threading import Lock

from prompt_toolkit import Application
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

_FLUSH_INTERVAL_SECONDS = 1 / 30
_OMITTED_MARKER = "[Earlier terminal output omitted]\n"


class TerminalView:
    """Own one full-screen transcript and one fixed single-line input buffer."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        completer: Completer | None = None,
        transcript_limit: int = 200_000,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._loop = loop
        self._transcript_limit = transcript_limit
        self._pending_chunks: list[str] = []
        self._pending_lock = Lock()
        self._flush_scheduled = False
        self._closed = False
        self._follow_tail = True
        self._status = "AGENT | IDLE"
        self._prompt = "mini-agent> "
        self.submissions: asyncio.Queue[str | None] = asyncio.Queue()

        self.transcript = TextArea(
            text="",
            read_only=True,
            focusable=False,
            wrap_lines=True,
            scrollbar=True,
            style="class:transcript",
        )
        self.input = TextArea(
            height=1,
            multiline=False,
            prompt=self._prompt_fragments,
            completer=completer,
            complete_while_typing=True,
            accept_handler=self._accept_input,
            style="class:input",
        )
        self.status_control = FormattedTextControl(self._status_fragments)
        self.status_window = Window(
            self.status_control,
            height=1,
            dont_extend_height=True,
            style="class:status",
        )
        separator = Window(height=1, char="-", style="class:separator")
        body = HSplit([self.transcript, separator, self.status_window, self.input])
        root = FloatContainer(
            content=body,
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8))],
        )
        self.application: Application[None] = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=self._key_bindings(),
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=_FLUSH_INTERVAL_SECONDS,
            max_render_postpone_time=0.01,
            style=Style.from_dict(
                {
                    "transcript": "bg:#101418 #d7dde5",
                    "separator": "#5f6b76",
                    "status": "bg:#20262d #9fc3e8",
                    "input": "bg:#171c21 #ffffff",
                    "prompt": "bold #62c7a5",
                }
            ),
            input=input,
            output=output,
        )

    @property
    def follow_tail(self) -> bool:
        return self._follow_tail

    @property
    def transcript_text(self) -> str:
        return self.transcript.buffer.text

    def write(self, text: str, end: str = "\n") -> None:
        value = f"{text}{end}"
        if not value or self._closed:
            return
        should_schedule = False
        with self._pending_lock:
            self._pending_chunks.append(value)
            if not self._flush_scheduled:
                self._flush_scheduled = True
                should_schedule = True
        if should_schedule:
            try:
                self._loop.call_soon_threadsafe(self._schedule_flush)
            except RuntimeError:
                pass

    def clear(self) -> None:
        if self._in_event_loop():
            self._clear_now()
        else:
            self._loop.call_soon_threadsafe(self._clear_now)

    def set_ui(self, *, status: str, prompt: str) -> None:
        self._status = status
        self._prompt = prompt
        self.application.invalidate()

    def flush_now(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    async def run_async(self) -> None:
        await self.application.run_async()

    def stop(self) -> None:
        self.flush_now()
        self._closed = True
        if self.application.is_running:
            self.application.exit()

    def scroll_page_up(self) -> None:
        self._follow_tail = False
        self._move_transcript_cursor(-self._page_height())

    def scroll_page_down(self) -> None:
        self._move_transcript_cursor(self._page_height())

    def scroll_lines(self, lines: int) -> None:
        if lines < 0:
            self._follow_tail = False
        self._move_transcript_cursor(lines)

    def follow_latest(self) -> None:
        self._follow_tail = True
        self.transcript.buffer.cursor_position = len(self.transcript.buffer.text)
        self.application.invalidate()

    def _schedule_flush(self) -> None:
        if self._closed:
            return
        self._loop.call_later(_FLUSH_INTERVAL_SECONDS, self._flush_pending)

    def _flush_pending(self) -> None:
        chunks = self._take_pending_chunks()
        if chunks:
            self._append_transcript("".join(chunks))

    def _take_pending_chunks(self) -> list[str]:
        with self._pending_lock:
            chunks = self._pending_chunks
            self._pending_chunks = []
            self._flush_scheduled = False
        return chunks

    def _append_transcript(self, value: str) -> None:
        buffer = self.transcript.buffer
        old_text = buffer.text
        old_cursor = buffer.cursor_position
        combined = f"{old_text}{value}"
        removed = 0
        if len(combined) > self._transcript_limit:
            keep = max(0, self._transcript_limit - len(_OMITTED_MARKER))
            removed = len(combined) - keep
            combined = f"{_OMITTED_MARKER}{combined[-keep:]}" if keep else ""
        marker_offset = len(_OMITTED_MARKER) if removed else 0
        cursor = len(combined) if self._follow_tail else max(0, old_cursor - removed + marker_offset)
        buffer.set_document(Document(combined, min(cursor, len(combined))), bypass_readonly=True)
        if self._follow_tail:
            buffer.cursor_position = len(combined)
        self.application.invalidate()

    def _clear_now(self) -> None:
        with self._pending_lock:
            self._pending_chunks = []
        self.transcript.buffer.set_document(Document(""), bypass_readonly=True)
        self._follow_tail = True
        self.application.invalidate()

    def _move_transcript_cursor(self, rows: int) -> None:
        buffer = self.transcript.buffer
        document = buffer.document
        target = max(0, min(document.line_count - 1, document.cursor_position_row + rows))
        buffer.cursor_position = document.translate_row_col_to_index(target, 0)
        if target >= document.line_count - 1:
            self._follow_tail = True
            buffer.cursor_position = len(buffer.text)
        self.application.invalidate()

    def _page_height(self) -> int:
        render_info = self.transcript.window.render_info
        return max(1, render_info.window_height - 1) if render_info is not None else 10

    def _accept_input(self, buffer) -> bool:
        value = buffer.text
        buffer.text = ""
        self.submissions.put_nowait(value)
        return True

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add(Keys.PageUp)
        def _page_up(_event) -> None:
            self.scroll_page_up()

        @bindings.add(Keys.PageDown)
        def _page_down(_event) -> None:
            self.scroll_page_down()

        @bindings.add(Keys.ScrollUp)
        def _scroll_up(_event) -> None:
            self.scroll_lines(-3)

        @bindings.add(Keys.ScrollDown)
        def _scroll_down(_event) -> None:
            self.scroll_lines(3)

        @bindings.add("c-end")
        def _follow_latest(_event) -> None:
            self.follow_latest()

        @bindings.add("c-c")
        def _control_c(event) -> None:
            if event.app.current_buffer.text:
                event.app.current_buffer.text = ""
                return
            self.submissions.put_nowait(None)

        @bindings.add("c-d")
        def _control_d(event) -> None:
            if event.app.current_buffer.text:
                event.app.current_buffer.delete()
                return
            self.submissions.put_nowait(None)

        return bindings

    def _status_fragments(self) -> FormattedText:
        suffix = " | PgUp/PgDn scroll" if not self._follow_tail else ""
        return FormattedText([("class:status", f" {self._status}{suffix}")])

    def _prompt_fragments(self) -> FormattedText:
        return FormattedText([("class:prompt", self._prompt)])

    def _in_event_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False
