# Bottom Bar Visual Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the unwanted separator and make the adjacent input and status rows visually distinct.

**Architecture:** Delete the `Rule` widget so the flexible transcript ends directly above the one-row input. Keep the input background `#171c21`, set the final-row status background to `#263442`, and verify both layout adjacency and computed colors in the Textual test app.

**Tech Stack:** Python 3.11+, Textual 8.x, pytest 8.x.

## Global Constraints

- The final order is transcript, input, status with no separator widget or reserved separator margin.
- Input background is exactly `#171c21`.
- Status background is exactly `#263442`.
- Input and status remain one row high.
- Preserve transcript copy feedback, normal input Paste, and unrelated working-tree changes.

---

### Task 1: Remove the separator and strengthen bottom-bar contrast

**Files:**
- Modify: `tests/test_tui_view.py:5,36-50`
- Modify: `src/mini_agent/tui/view.py:12,65-99,138-146`

**Interfaces:**
- Consumes: `TerminalView.compose()`, Textual `Rule`, `Widget.region`, and `Color.parse()`.
- Produces: adjacent `transcript`, `input`, and `status_line` regions with no `Rule`; configured backgrounds `#171c21` and `#263442`.

- [ ] **Step 1: Write the failing layout and color assertions**

Import `Color` and `Rule`, then extend the existing layout test:

```python
from textual.color import Color
from textual.widgets import Label, Rule

children = list(view.screen.children)
assert children[-2:] == [view.input, view.status_line]
assert list(view.query(Rule)) == []
assert view.transcript.region.bottom == view.input.region.y
assert view.input.region.bottom == view.status_line.region.y
assert view.input.styles.background == Color.parse("#171c21")
assert view.status_line.styles.background == Color.parse("#263442")
```

Keep the existing one-row height, zero input bottom margin, transcript, and no-`Label` assertions.

- [ ] **Step 2: Run the layout test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py::test_textual_view_reserves_bottom_input_and_scrollable_transcript
```

Expected: FAIL because a `Rule` is composed, its default margins separate the transcript from input, and the status background is still `#20262d`.

- [ ] **Step 3: Remove the Rule and set the status color**

In `src/mini_agent/tui/view.py`, remove `Rule` from the widget import, change the status CSS, and remove the separator yield:

```python
from textual.widgets import Input, OptionList, Static, TextArea

#status { height: 1; padding: 0 1; background: #263442; color: #9fc3e8; }

def compose(self) -> ComposeResult:
    yield self.transcript
    yield self.question_header
    yield self.question_menu
    yield self.completion_menu
    yield self.input
    yield self.status_line
```

- [ ] **Step 4: Run the TUI tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_tui_view.py
```

Expected: all TUI view tests pass.

- [ ] **Step 5: Run full regression and diff verification**

Run:

```powershell
python -m pytest -q
git diff --check
```

Expected: pytest reports zero failures and `git diff --check` exits 0. Leave the implementation uncommitted in the current workspace, as previously requested.
