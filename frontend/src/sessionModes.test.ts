import { describe, expect, it } from "vitest";
import { loadSessionModes, MODE_STORAGE_KEY, saveSessionModes, type SimpleStorage } from "./app/sessionModes";

function storage(initial: string | null = null): SimpleStorage {
  let value = initial;
  return {
    getItem: () => value,
    setItem: (_key, next) => { value = next; },
  };
}

describe("per-session mode memory", () => {
  it("round-trips independent agent and plan selections", () => {
    const target = storage();
    const modes = { session_agent: "agent" as const, session_plan: "plan" as const };
    saveSessionModes(target, modes);
    expect(target.getItem(MODE_STORAGE_KEY)).toContain("session_plan");
    expect(loadSessionModes(target)).toEqual(modes);
  });

  it("falls back to an empty mapping for malformed browser data", () => {
    expect(loadSessionModes(storage("not-json"))).toEqual({});
  });
});
