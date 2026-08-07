import { describe, expect, it } from "vitest";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "./commandCompletion";

describe("slash command completion", () => {
  it("does not expose removed commands", () => {
    expect(commandSuggestions("/per")).toEqual([]);
    expect(commandSuggestions("/ag")).toEqual([]);
    expect(commandSuggestions("/permission ")).toEqual([]);
    expect(commandSuggestions("/display")).toEqual([]);
  });

  it("cycles retained commands and preserves argument input", () => {
    const matches = commandSuggestions("/h");
    expect(matches.map((command) => command.name)).toEqual(["/help"]);
    expect(nextCommandIndex(0, 1, matches.length)).toBe(0);
    expect(nextCommandIndex(0, -1, matches.length)).toBe(0);
    expect(completionText(commandSuggestions("/new")[0])).toBe("/new ");
    expect(completionText(commandSuggestions("/help")[0])).toBe("/help");
  });

  it("separates completion from execution and respects IME/newline keys", () => {
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: true })).toEqual({ type: "complete" });
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: false, menuVisible: false })).toEqual({ type: "send" });
    expect(commandKeyAction({ key: "Enter", shiftKey: true, isComposing: false, menuVisible: true })).toEqual({ type: "none" });
    expect(commandKeyAction({ key: "Enter", shiftKey: false, isComposing: true, menuVisible: true })).toEqual({ type: "none" });
  });
});