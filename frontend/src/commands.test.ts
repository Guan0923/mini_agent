import { describe, expect, it } from "vitest";
import { COMMANDS, HELP_TEXT, parseCommand } from "./commands";

describe("web command catalog", () => {
  it("keeps plan and agent out of the visible menu while retaining aliases", () => {
    expect(COMMANDS.some((command) => command.name === "/plan")).toBe(false);
    expect(COMMANDS.some((command) => command.name === "/agent")).toBe(false);
    expect(parseCommand("/plan")).toEqual({ name: "/plan", argument: "" });
    expect(parseCommand("/agent")).toEqual({ name: "/agent", argument: "" });
  });

  it("parses argument commands and documents the full browser catalog", () => {
    expect(parseCommand("/display verbose")).toEqual({ name: "/display", argument: "verbose" });
    expect(parseCommand("/new Research notes")).toEqual({ name: "/new", argument: "Research notes" });
    expect(parseCommand("ordinary task")).toBeNull();
    expect(HELP_TEXT).toContain("/compact");
    expect(HELP_TEXT).toContain("/trace");
  });
});
