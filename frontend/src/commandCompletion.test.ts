import { describe, expect, it } from "vitest";
import {
  commandKeyAction,
  commandSuggestions,
  commandTrigger,
  completionText,
  nextCommandIndex,
} from "./commands/completion";

describe("slash command completion", () => {
  it("does not expose removed commands", () => {
    expect(commandSuggestions("/new")).toEqual([]);
    expect(commandSuggestions("/per")).toEqual([]);
    expect(commandSuggestions("/ag")).toEqual([]);
    expect(commandSuggestions("/permission ")).toEqual([]);
    expect(commandSuggestions("/display")).toEqual([]);
  });

  it("cycles retained commands and completes only the selected command", () => {
    const matches = commandSuggestions("/h");
    expect(matches.map((command) => command.name)).toEqual(["/help"]);
    expect(nextCommandIndex(0, 1, matches.length)).toBe(0);
    expect(nextCommandIndex(0, -1, matches.length)).toBe(0);
    expect(completionText(commandSuggestions("/help")[0])).toBe("/help");
  });

  it("detects an independent token at the start or after whitespace", () => {
    expect(commandTrigger("/he", 3)).toEqual({ query: "/he", start: 0, end: 3 });
    expect(commandTrigger("请执行 /he 后续", 7)).toEqual({ query: "/he", start: 4, end: 7 });
    expect(commandTrigger("换行\n/tr", 6)).toEqual({ query: "/tr", start: 3, end: 6 });
    expect(commandTrigger("word/he", 7)).toBeNull();
    expect(commandTrigger("https://host", 12)).toBeNull();
    expect(commandTrigger(" /one/two", 9)).toBeNull();
  });

  it("separates completion from execution and respects IME/newline keys", () => {
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "execute" });
    expect(commandKeyAction({ key: "Tab", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "complete" });
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: false })).toEqual({ type: "send" });
    expect(commandKeyAction({ key: "Enter", shiftKey: true, isComposing: false, menuVisible: true })).toEqual({ type: "none" });
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: true, menuVisible: true })).toEqual({ type: "none" });
    expect(commandKeyAction({ key: "ArrowDown", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "move", direction: 1 });
    expect(commandKeyAction({ key: "ArrowUp", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "move", direction: -1 });
    expect(commandKeyAction({ key: "Escape", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "dismiss" });
  });
});
