# Transcript Copy Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both transcript right-click copy paths collapse the selection, restore input focus, and show a race-safe 1.5-second character-count notice.

**Architecture:** Keep clipboard behavior centralized in `TerminalView.copy_transcript_selection()`. Separate durable status rendering from transient notice handling, store one replaceable Textual `Timer`, and reject any callback whose timer is no longer current so runtime or scroll refreshes cannot be overwritten.

**Tech Stack:** Python 3.11+, Textual 8.x, pytest 8.x, asyncio-based Textual pilot tests.

## Global Constraints

- The success message is exactly `COPIED — N characters` and remains visible for 1.5 seconds unless replaced or invalidated by a normal status refresh.
- A non-empty selection is copied exactly once, collapsed at its existing end position, and followed by input focus restoration.
- An empty selection changes neither clipboard nor status.
- Existing Agent, Plan, Running, approval, permission, and scroll status composition remains authoritative.
- Preserve all unrelated working-tree changes, including the existing stop/idempotency edits.

---

### Task 1: Race-safe transcript copy feedback

**Files:**
- Modify: `tests/test_tui_view.py:252`
- Modify: `src/mini_agent/tui/view.py:1-505`

**Interfaces:**
- Consumes: `TranscriptTextArea.on_mouse_down()`, `TerminalInput.on_event()`, `TerminalView.set_ui(status: str)`, Textual `Timer`, and `Selection.cursor(location)`.
- Produces: `TerminalView.copy_transcript_selection() -> bool`, `_show_copy_notice(character_count: int) -> None`, `_invalidate_copy_notice() -> None`, `_restore_status_after_copy(timer: Timer | None) -> None`, and `_render_status() -> None`.

- [ ] **Step 1: Extend both existing copy-path tests and add timer/empty-selection coverage**

Replace the two existing copy tests and append the two new tests below:

```python
def test_right_click_copies_selection_clears_it_and_shows_feedback(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.transcript.load_text("copy this")
            view.transcript.select_all()
            selection_end = view.transcript.selection.end

            await pilot.click("#transcript", button=3)
            await pilot.pause()

            assert copied == ["copy this"]
            assert view.transcript.selection.is_empty
            assert view.transcript.selection.end == selection_end
            assert view.focused is view.input
            assert str(view.status_line.content) == " COPIED — 9 characters"

    asyncio.run(scenario())


def test_terminal_right_click_paste_copies_selection_clears_it_and_shows_feedback(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.input.value = "draft"
            view.transcript.load_text("selected output")
            view.transcript.select_all()
            selection_end = view.transcript.selection.end

            view.input.post_message(events.Paste("old clipboard contents"))
            await pilot.pause()

            assert copied == ["selected output"]
            assert view.transcript.selection.is_empty
            assert view.transcript.selection.end == selection_end
            assert view.focused is view.input
            assert view.input.value == "draft"
            assert str(view.status_line.content) == " COPIED — 15 characters"

    asyncio.run(scenario())


def test_copy_notice_restores_latest_status_without_stale_timer_overwrite(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        monkeypatch.setattr(view, "copy_to_clipboard", lambda _text: None)
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | RUNNING")
            view.transcript.load_text("copy")
            view.transcript.select_all()
            view.copy_transcript_selection()
            assert str(view.status_line.content) == " COPIED — 4 characters"

            view.set_ui(status="PLAN | RUNNING")
            assert str(view.status_line.content) == " PLAN | RUNNING"
            await asyncio.sleep(1.6)
            await pilot.pause()

            assert str(view.status_line.content) == " PLAN | RUNNING"

    asyncio.run(scenario())


def test_copy_without_selection_leaves_clipboard_and_status_unchanged(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.set_ui(status="AGENT | IDLE")
            view.transcript.load_text("not selected")

            await pilot.click("#transcript", button=3)
            await pilot.pause()

            assert copied == []
            assert str(view.status_line.content) == " AGENT | IDLE"

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py -k "right_click or copy_notice or copy_without_selection"
```

Expected: failures show that native right-click does not clear its selection and neither copy route displays `COPIED — N characters`.

- [ ] **Step 3: Add the replaceable timer and centralized copy behavior**

In `src/mini_agent/tui/view.py`, import `Timer`, add the duration constant and instance field, remove the `clear` parameter from both the compatibility caller and `copy_transcript_selection()`, and implement the status helpers as follows:

```python
from textual.timer import Timer

_COPY_NOTICE_SECONDS = 1.5

# TerminalInput.on_event
if isinstance(view, TerminalView) and view.copy_transcript_selection():

# TerminalView.__init__
self._copy_notice_timer: Timer | None = None

# TerminalView.set_ui update callback
self._status = status
self._refresh_status()

def copy_transcript_selection(self) -> bool:
    selected = self.transcript.selected_text
    if not selected:
        self.input.focus()
        return False
    selection_end = self.transcript.selection.end
    self.copy_to_clipboard(selected)
    self.transcript.selection = Selection.cursor(selection_end)
    self._show_copy_notice(len(selected))
    self.input.focus()
    return True

def _show_copy_notice(self, character_count: int) -> None:
    self._invalidate_copy_notice()
    self.status_line.update(f" COPIED — {character_count} characters")
    timer: Timer | None = None

    def restore() -> None:
        self._restore_status_after_copy(timer)

    timer = self.set_timer(_COPY_NOTICE_SECONDS, restore)
    self._copy_notice_timer = timer

def _invalidate_copy_notice(self) -> None:
    if self._copy_notice_timer is not None:
        self._copy_notice_timer.stop()
        self._copy_notice_timer = None

def _restore_status_after_copy(self, timer: Timer | None) -> None:
    if timer is not self._copy_notice_timer:
        return
    self._copy_notice_timer = None
    self._render_status()

def _refresh_status(self) -> None:
    self._invalidate_copy_notice()
    self._render_status()

def _render_status(self) -> None:
    suffix = " | PgUp/PgDn scroll" if not self._follow_tail else ""
    self.status_line.update(f" {self._status}{suffix}")
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py -k "right_click or copy_notice or copy_without_selection"
```

Expected: all selected tests pass.

- [ ] **Step 5: Run formatting and complete regression verification**

Run:

```powershell
python -m ruff check src/mini_agent/tui/view.py tests/test_tui_view.py
python -m pytest -q
```

Expected: Ruff exits 0 and the complete pytest suite reports zero failures.

- [ ] **Step 6: Review the diff and commit only this feature's files**

Run:

```powershell
git diff --check
git diff -- src/mini_agent/tui/view.py tests/test_tui_view.py
git add src/mini_agent/tui/view.py tests/test_tui_view.py docs/superpowers/plans/2026-07-17-transcript-copy-feedback.md
git commit -m "feat: add transcript copy feedback"
```

Before staging, distinguish the pre-existing stop/idempotency hunks from the copy-feedback hunks. If they cannot be staged independently without risking user work, leave the feature uncommitted and report that clearly instead of including unrelated changes.
