# TUI Multiline Input, Command Hints, and Permission Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Ctrl+J multiline editing with a four-row input cap, descriptive slash-command suggestions, and the active permission mode on every TUI status.

**Architecture:** Introduce a focused `TextArea` adapter inside `tui/view.py` that preserves the value/cursor/submission surface consumed by `TerminalView`. Keep completion data in the existing shared command catalog and compose permission text only at the TUI application boundary.

**Tech Stack:** Python 3.11+, Textual 8.2, asyncio, pytest.

## Global Constraints

- Enter submits; Ctrl+J inserts a newline at the current selection or cursor.
- Input height follows wrapped content from one through four rows, then scrolls internally.
- Completion descriptions are display-only; acceptance inserts only the command.
- Runtime, provider, session, steering, queue, Esc, Ctrl+C, and approval contracts remain unchanged.
- Preserve all existing uncommitted workspace changes and do not stage or commit overlapping source files automatically.

---

### Task 1: Multiline TextArea adapter

**Files:**
- Modify: `src/mini_agent/tui/view.py`
- Test: `tests/test_tui_view.py`

**Interfaces:**
- Produces: `TerminalInput(TextArea)` with `value: str`, `cursor_position: int`, and nested `Submitted` message carrying `input` and `value`.
- Consumes: `Document.get_index_from_location`, `Document.get_location_from_index`, `TextArea.replace`, and `wrapped_document.height`.

- [ ] Add failing Textual tests proving Enter submits, Ctrl+J inserts without submitting, insertion respects the cursor, input grows to four rows, and a fifth line keeps height four.
- [ ] Run `python -m pytest -q tests/test_tui_view.py -k "multiline or input_acceptance"`; expect failures because the current widget is single-line.
- [ ] Implement `TerminalInput` and migrate `Input.Changed`/`Input.Submitted` handlers to `TextArea.Changed`/`TerminalInput.Submitted`:

```python
class TerminalInput(TextArea):
    class Submitted(Message):
        def __init__(self, input: "TerminalInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)
        self.cursor_location = self.document.get_location_from_index(len(value))

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, value: int) -> None:
        self.cursor_location = self.document.get_location_from_index(value)
```

- [ ] Intercept Enter to post `Submitted`, intercept Ctrl+J to replace the current selection with `\n`, and resize from `max(1, min(wrapped_document.height, 4))` after changes and terminal resize.
- [ ] Run `python -m pytest -q tests/test_tui_view.py`; expect all view tests to pass.

### Task 2: Descriptive command completion

**Files:**
- Modify: `src/mini_agent/tui/view.py`
- Test: `tests/test_tui_view.py`

**Interfaces:**
- Consumes: existing `CommandSuggestion.value` and `CommandSuggestion.description`.
- Produces: display prompt `<value> — <description>` while `_accept_completion()` continues inserting `value` only.

- [ ] Update the existing completion test to expect `/plan — Create a plan and open Plan Review.` and `/permission — Choose the in-memory tool approval mode.`, then verify it fails.
- [ ] Render options with `Option(f"{item.value} — {item.description}", id=str(index))` without changing `_suggestions` or `_accept_completion()`.
- [ ] Run `python -m pytest -q tests/test_tui_view.py -k completion`; expect all completion tests to pass.

### Task 3: Permission-aware status and documentation

**Files:**
- Modify: `src/mini_agent/tui/cli.py`
- Modify: `README.md`
- Modify: `docs/development.md`
- Test: `tests/test_cli.py`
- Test: `tests/test_tui_view.py`

**Interfaces:**
- Consumes: `TerminalApproval.permission_mode` values `approval_for_me` and `full_access`.
- Produces: status strings ending in `PERMISSION: APPROVAL FOR ME` or `PERMISSION: FULL ACCESS`.

- [ ] Add failing tests for idle/running/review status strings and for a `/permission` change refreshing the displayed mode.
- [ ] Add a private TUI formatter that maps the current permission mode to uppercase words and appends it in `_update_view_state` for every state.
- [ ] Update fake views to test state markers with containment rather than relying on `status.endswith("RUNNING")`, because permission is now the suffix.
- [ ] Document Enter, Ctrl+J, four-row growth, descriptive completion, and permission status in the TUI sections.
- [ ] Run `python -m pytest -q tests/test_cli.py tests/test_tui_view.py`; expect all targeted tests to pass.
- [ ] Run `python -m pytest -q` and `git diff --check`; expect zero failures and no whitespace errors.
