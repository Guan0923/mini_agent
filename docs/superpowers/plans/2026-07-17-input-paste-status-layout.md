# Input Paste and Bottom Status Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve normal input Paste behavior and place the status bar on the terminal's final row below the input.

**Architecture:** Remove the custom input Paste interception so Textual's standard `Input` owns all Paste events, while transcript right-click remains the only copy trigger. Keep the transcript as the flexible-height widget, then compose the input immediately before the fixed-height status widget.

**Tech Stack:** Python 3.11+, Textual 8.x, pytest 8.x, asyncio-based Textual pilot tests.

## Global Constraints

- Pasting in the input always inserts clipboard text into the input, even while transcript text remains selected.
- Transcript copying remains centralized in `TerminalView.copy_transcript_selection()` and is triggered only by transcript right-click.
- The terminal's final two rows are input followed by status.
- The status retains Agent, Plan, Running, approval, permission, scroll, and transient copy feedback behavior.
- Preserve unrelated working-tree changes, including the existing stop/idempotency work.

---

### Task 1: Restore normal input Paste behavior

**Files:**
- Modify: `tests/test_tui_view.py:273-296`
- Modify: `src/mini_agent/tui/view.py:48-60,148`

**Interfaces:**
- Consumes: Textual `Input._on_paste(events.Paste)` and `TranscriptTextArea.on_mouse_down()`.
- Produces: `TerminalView.input: Input`; `TerminalView.copy_transcript_selection() -> bool` remains unchanged and has no Paste caller.

- [ ] **Step 1: Replace the compatibility-copy test with a failing input-Paste regression test**

```python
def test_input_paste_inserts_text_without_copying_transcript_selection(monkeypatch) -> None:
    async def scenario() -> None:
        view = TerminalView()
        copied: list[str] = []
        monkeypatch.setattr(view, "copy_to_clipboard", copied.append)
        async with view.run_test() as pilot:
            view.input.value = "draft"
            view.input.cursor_position = len(view.input.value)
            view.transcript.load_text("selected output")
            view.transcript.select_all()
            transcript_selection = view.transcript.selection

            view.input.post_message(events.Paste(" pasted"))
            await pilot.pause()

            assert copied == []
            assert view.input.value == "draft pasted"
            assert view.transcript.selection == transcript_selection
            assert str(view.status_line.content) == " AGENT | IDLE"

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py::test_input_paste_inserts_text_without_copying_transcript_selection
```

Expected: FAIL because `TerminalInput.on_event()` calls `copy_transcript_selection()`, leaves the input as `draft`, and changes the transcript selection/status.

- [ ] **Step 3: Remove the Paste interception**

Delete the `TerminalInput` subclass and instantiate Textual's standard input directly:

```python
self.input = Input(id="input")
```

Do not change `TranscriptTextArea.on_mouse_down()` or `TerminalView.copy_transcript_selection()`.

- [ ] **Step 4: Run the regression and transcript-copy tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py -k "input_paste or right_click_copies"
```

Expected: both selected tests pass.

### Task 2: Place status on the terminal's final row

**Files:**
- Modify: `tests/test_tui_view.py:36-47`
- Modify: `src/mini_agent/tui/view.py:79-100,150-158`

**Interfaces:**
- Consumes: `TerminalView.compose()` and Textual vertical layout sizing.
- Produces: screen child order ending in `TerminalView.input`, `TerminalView.status_line`; input bottom margin is zero.

- [ ] **Step 1: Strengthen the layout test**

Update the existing layout assertions:

```python
children = list(view.screen.children)
assert children[-2:] == [view.input, view.status_line]
assert view.input.styles.height.value == 1
assert view.input.styles.margin.bottom == 0
assert view.status_line.styles.height.value == 1
```

Keep the existing transcript read-only, soft-wrap, scroll, and no-`Label` assertions.

- [ ] **Step 2: Run the layout test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py::test_textual_view_reserves_bottom_input_and_scrollable_transcript
```

Expected: FAIL because current child order ends in status then input and the input bottom margin is 1.

- [ ] **Step 3: Reorder the widgets and remove the bottom margin**

Change the input CSS and the end of `compose()` to:

```python
#input {
    width: 100%;
    height: 1;
    margin-bottom: 0;
    border: none;
    padding: 0;
    background: #171c21;
    color: white;
}

def compose(self) -> ComposeResult:
    yield self.transcript
    yield self.question_header
    yield self.question_menu
    yield self.completion_menu
    yield Rule(id="separator")
    yield self.input
    yield self.status_line
```

- [ ] **Step 4: Run the complete TUI view tests**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py
```

Expected: all `test_tui_view.py` tests pass.

- [ ] **Step 5: Run complete regression and diff verification**

Run:

```powershell
python -m pytest -q
git diff --check
```

Expected: pytest reports zero failures and `git diff --check` exits 0. Ruff is optional because it is not installed in the current environment.

- [ ] **Step 6: Leave the implementation in the current working tree**

Do not create a worktree, branch, merge, push, or stage unrelated files. Report the modified files and verification results to the user.
