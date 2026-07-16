# Transcript Copy Feedback Design

## Behavior

- Right-clicking a non-empty transcript selection copies it to the system clipboard, collapses the selection, and restores focus to the input.
- The status bar shows `COPIED — N characters` for 1.5 seconds, then restores the current Agent, Plan, Running, approval, permission, or scroll status.
- Right-clicking without a selection does not change the clipboard or status bar.

## Implementation

- Keep copy behavior in `TerminalView.copy_transcript_selection()` so the native right-click and terminal Paste compatibility paths remain consistent.
- Use a replaceable Textual timer for the transient notice. A newer copy or any normal status refresh invalidates the older restore callback so stale timers cannot overwrite current runtime state.
- Preserve the existing transcript selection cursor position when collapsing the selection and keep the input focused.

## Tests

- Verify both copy paths copy once, clear the selection, restore input focus, and show the character count.
- Verify the notice restores the latest status after 1.5 seconds.
- Verify an empty selection neither changes the clipboard nor displays a success notice.
