# Transcript Copy Feedback Design

## Behavior

- Right-clicking a non-empty transcript selection copies it to the system clipboard, collapses the selection, and restores focus to the input.
- Pasting in the input always inserts clipboard text into the input, even while transcript text remains selected.
- The status bar shows `COPIED — N characters` for 1.5 seconds, then restores the current Agent, Plan, Running, approval, permission, or scroll status.
- Right-clicking without a selection does not change the clipboard or status bar.
- The terminal's bottom rows are ordered as input followed by status, making the status bar the final row below the transcript.

## Implementation

- Keep copy behavior in `TerminalView.copy_transcript_selection()` and call it only from the transcript's native right-click handler.
- Remove the input Paste interception and use Textual's normal `Input` Paste handling. Transcript copying is triggered only by a right-click in the transcript.
- Use a replaceable Textual timer for the transient notice. A newer copy or any normal status refresh invalidates the older restore callback so stale timers cannot overwrite current runtime state.
- Preserve the existing transcript selection cursor position when collapsing the selection and keep the input focused.
- Compose the input before the status widget and remove the input's bottom margin so the status occupies the terminal's last row.

## Tests

- Verify transcript right-click copies once, clears the selection, restores input focus, and shows the character count.
- Verify input Paste inserts text without copying or changing the transcript selection.
- Verify the notice restores the latest status after 1.5 seconds.
- Verify an empty selection neither changes the clipboard nor displays a success notice.
- Verify the composed widget order ends with input then status and reserves no bottom margin below the input.
