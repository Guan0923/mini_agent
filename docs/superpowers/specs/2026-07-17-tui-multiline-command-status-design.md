# TUI Multiline Input, Command Hints, and Permission Status Design

## Goal

Improve the Textual TUI input and status display without changing runtime or provider interfaces.

## Input behavior

- Replace the single-line input widget with a small `TextArea`-based adapter that preserves the value, cursor, placeholder, focus, completion, questionnaire, and submission behavior expected by `TerminalView`.
- Enter submits the complete text. Ctrl+J inserts `\n` at the current selection or cursor without submitting.
- The input grows from one to four visible rows using the wrapped document height, so both explicit newlines and soft wrapping count. At four rows it remains fixed and scrolls internally.
- Submission clears the editor. Empty input, multiline queueing, Esc cancellation, Ctrl+C, Ctrl+D, Plan questionnaires, and slash completion retain their current semantics.

## Command completion

- Render each completion option as `<command> — <description>` using the existing shared `COMMAND_DEFINITIONS` catalog.
- Accepting an option inserts only the slash command value, never the description.

## Permission status

- Every normal status line includes the current process-local permission mode, for example `AGENT | RUNNING | PERMISSION: APPROVAL FOR ME` or `AGENT | IDLE | PERMISSION: FULL ACCESS`.
- Review, questionnaire, permission-selection, and cancelling states also include the same permission suffix.
- Changing permission through `/permission` refreshes the next status render immediately. Temporary copy feedback restores the latest full status afterward.

## Testing and compatibility

- Add Textual tests for Ctrl+J insertion, Enter submission, cursor-aware multiline editing, one-to-four-row growth, overflow scrolling, completion descriptions, and command-only insertion.
- Add CLI tests for both permission modes across idle/running/review states and permission changes.
- Preserve the existing runtime steering API, TUI run queue, cooperative Esc cancellation, session persistence, and provider behavior.
