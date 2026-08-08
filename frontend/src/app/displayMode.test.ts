import { describe, expect, it } from "vitest";
import { effectiveDisplayMode } from "./displayMode";

describe("display mode compatibility", () => {
  it("keeps developer details in development builds", () => {
    expect(effectiveDisplayMode("developer", true)).toBe("developer");
  });

  it("downgrades persisted developer settings in production", () => {
    expect(effectiveDisplayMode("developer", false)).toBe("verbose");
  });
});
