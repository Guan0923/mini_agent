import type { FileReference, SessionFileInfo } from "../types";

export interface FileTrigger {
  /** The text typed after the triggering `@` (may be empty). */
  query: string;
  /** Index of the `@` character in the input. */
  start: number;
  /** Index just after the query token. */
  end: number;
}

/**
 * Detect a file-completion trigger at the caret: an `@` at the start of the
 * input or right after a whitespace.  Anything else (mid-word, after a
 * non-space character) is left to the slash-command or plain text flows.
 */
export function fileTrigger(input: string, caret: number): FileTrigger | null {
  const head = input.slice(0, caret);
  const match = /(?:^|\s)@([^\s@]*)$/.exec(head);
  if (!match) return null;
  const start = match.index + match[0].length - match[1].length - 1;
  return { query: match[1], start, end: caret };
}

/** Build the text inserted for one completion: `@path` or `@"path with space"`. */
export function completionToken(path: string): string {
  return /[\s"]/.test(path) ? `@"${path.replace(/"/g, '\\"')}"` : `@${path}`;
}

/** Replace the trigger range with the token and return the new caret. */
export function insertToken(input: string, trigger: FileTrigger, token: string): { value: string; caret: number } {
  const value = `${input.slice(0, trigger.start)}${token}${input.slice(trigger.end)}`;
  return { value, caret: trigger.start + token.length };
}

/** A completion candidate grouped by its search source. */
export interface FileCandidate {
  file: SessionFileInfo;
  reference: FileReference;
  label: string;
  sourceLabel: "项目文件" | "会话上传";
}

export function toCandidates(files: SessionFileInfo[]): FileCandidate[] {
  return files.map((file) => ({
    file,
    reference: { source: file.source, path: file.path },
    label: file.name,
    sourceLabel: file.source === "upload" ? "会话上传" : "项目文件",
  }));
}

export type FileKeyAction =
  | { type: "complete" }
  | { type: "dismiss" }
  | { type: "move"; direction: -1 | 1 }
  | { type: "none" };

/** Keyboard handling for the file menu, mirroring the command menu. */
export function fileKeyAction({
  key,
  shiftKey,
  isComposing,
  menuVisible,
}: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  menuVisible: boolean;
}): FileKeyAction {
  if (isComposing) return { type: "none" };
  if (menuVisible && (key === "ArrowDown" || key === "ArrowUp")) {
    return { type: "move", direction: key === "ArrowDown" ? 1 : -1 };
  }
  if (menuVisible && key === "Escape") return { type: "dismiss" };
  if (menuVisible && (key === "Enter" || key === "Tab") && !shiftKey) return { type: "complete" };
  return { type: "none" };
}
