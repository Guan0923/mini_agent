import { describe, expect, it } from "vitest";
import { COMMANDS, HELP_TEXT, parseCommand } from "./commands";

describe("web command catalog", () => {
  it("rejects unsupported commands at browser entry points", () => {
    const removed = ["/permission", "/sessions", "/fork", "/benchmark", "/history", "/clear", "/tools", "/agent", "/plan", "/resume", "/time", "/display"];
    for (const command of removed) {
      expect(COMMANDS.some((item) => item.name === command)).toBe(false);
      expect(parseCommand(command)).toBeNull();
    }
  });

  it("parses retained commands and documents only the browser catalog", () => {
    expect(parseCommand("/help")).toEqual({ name: "/help", argument: "" });
    expect(parseCommand("/new Research notes")).toEqual({ name: "/new", argument: "Research notes" });
    expect(parseCommand("/skills")).toEqual({ name: "/skills", argument: "" });
    expect(parseCommand("/compact")).toEqual({ name: "/compact", argument: "" });
    expect(parseCommand("/trace")).toEqual({ name: "/trace", argument: "" });
    expect(parseCommand("ordinary task")).toBeNull();
    expect(HELP_TEXT).toContain("/compact");
    expect(HELP_TEXT).toContain("/trace");
    expect(HELP_TEXT).not.toContain("/display");
  });
});
