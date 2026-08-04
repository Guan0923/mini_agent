import { describe, expect, it } from "vitest";
import { commandKeyAction, commandSuggestions, completionText, nextCommandIndex } from "./commandCompletion";

describe("slash command completion", () => {
  it("matches prefixes without exposing compatibility aliases", () => {
    expect(commandSuggestions("/per").map((command) => command.name)).toEqual(["/permission"]);
    expect(commandSuggestions("/ag")).toEqual([]);
    expect(commandSuggestions("/permission ")).toEqual([]);
  });

  it("cycles the highlighted command and preserves argument input", () => {
    const matches = commandSuggestions("/h");
    expect(matches.map((command) => command.name)).toEqual(["/history", "/help"]);
    expect(nextCommandIndex(0, 1, matches.length)).toBe(1);
    expect(nextCommandIndex(0, -1, matches.length)).toBe(1);
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
