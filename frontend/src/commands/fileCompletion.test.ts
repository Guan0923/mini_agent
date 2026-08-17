import { describe, expect, it } from "vitest";
import {
  completionToken,
  fileKeyAction,
  fileTrigger,
  insertToken,
  toCandidates,
} from "./fileCompletion";
import type { SessionFileInfo } from "../types";

const uploadFile = (path: string): SessionFileInfo => ({
  source: "upload",
  path,
  name: path.split("/").pop() ?? path,
  size: 10,
  mime: "text/plain",
  mtime: "2026-01-01T00:00:00+00:00",
  is_image: false,
});

describe("file completion triggers", () => {
  it("triggers at the start of the input", () => {
    expect(fileTrigger("@re", 3)).toEqual({ query: "re", start: 0, end: 3 });
    expect(fileTrigger("@", 1)).toEqual({ query: "", start: 0, end: 1 });
  });

  it("triggers after whitespace and respects the caret", () => {
    expect(fileTrigger("请查看 @re", 7)).toEqual({ query: "re", start: 4, end: 7 });
    expect(fileTrigger("请查看 @re", 5)).toEqual({ query: "", start: 4, end: 5 });
    // "换行\n@fil" with the caret after "l": the query is only what precedes
    // the caret.
    expect(fileTrigger("换行\n@file", 7)).toEqual({ query: "fil", start: 3, end: 7 });
  });

  it("does not trigger mid-word or after non-space text", () => {
    expect(fileTrigger("email@example.com", 12)).toBeNull();
    expect(fileTrigger("abc@def", 7)).toBeNull();
    expect(fileTrigger("no-at-sign", 10)).toBeNull();
    // A second @ mid-token does not start a new completion without whitespace.
    expect(fileTrigger("@one@two", 5)).toBeNull();
    // Typing a space ends the current completion entirely.
    expect(fileTrigger("@first second", 7)).toBeNull();
  });

  it("keeps the query bounded by the next whitespace or @", () => {
    expect(fileTrigger("@first second", 6)).toEqual({ query: "first", start: 0, end: 6 });
  });
});

describe("file completion tokens and insertion", () => {
  it("quotes paths with spaces", () => {
    expect(completionToken("docs/readme.md")).toBe("@docs/readme.md");
    expect(completionToken("my notes.txt")).toBe('@"my notes.txt"');
    expect(completionToken('say "hi".txt')).toBe('@"say \\"hi\\".txt"');
  });

  it("replaces only the trigger range", () => {
    const result = insertToken("请查看 @re 的内容", { query: "re", start: 4, end: 7 }, "@readme.md");
    expect(result.value).toBe("请查看 @readme.md 的内容");
    expect(result.caret).toBe(4 + "@readme.md".length);
  });
  it("maps search results to candidates with source labels", () => {
    const candidates = toCandidates([uploadFile("a.txt"), { ...uploadFile("b.txt"), source: "project" }]);
    expect(candidates[0].sourceLabel).toBe("会话上传");
    expect(candidates[1].sourceLabel).toBe("项目文件");
    expect(candidates[1].reference).toEqual({ source: "project", path: "b.txt" });
  });
});

describe("file completion keyboard actions", () => {
  it("navigates, completes, and dismisses like the command menu", () => {
    expect(fileKeyAction({ key: "ArrowDown", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "move", direction: 1 });
    expect(fileKeyAction({ key: "ArrowUp", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "move", direction: -1 });
    expect(fileKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "complete" });
    expect(fileKeyAction({ key: "Tab", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "complete" });
    expect(fileKeyAction({ key: "Escape", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "dismiss" });
    expect(fileKeyAction({ key: "Enter", shiftKey: true, isComposing: false, menuVisible: true })).toEqual({ type: "none" });
  });

  it("respects IME composition and hidden menus", () => {
    expect(fileKeyAction({ key: "Enter", shiftKey: false, isComposing: true, menuVisible: true })).toEqual({ type: "none" });
    expect(fileKeyAction({ key: "ArrowDown", shiftKey: false, isComposing: false, menuVisible: false })).toEqual({ type: "none" });
    expect(fileKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: false })).toEqual({ type: "none" });
  });
});
