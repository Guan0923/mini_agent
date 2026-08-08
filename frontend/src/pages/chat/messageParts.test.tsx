import { describe, expect, it } from "vitest";
import { summarizeThinking } from "./messageParts";

describe("runtime thinking summary", () => {
  it("uses the first non-empty paragraph and trims its edges", () => {
    expect(summarizeThinking("\n\n  第一段思考  \n\n第二段")).toBe("第一段思考");
  });

  it("keeps exactly one hundred Unicode code points without an ellipsis", () => {
    const value = "中😀".repeat(50);
    expect(Array.from(summarizeThinking(value))).toHaveLength(100);
    expect(summarizeThinking(value)).toBe(value);
  });

  it("adds five ASCII periods only when the paragraph is longer", () => {
    const value = "中文😀".repeat(51);
    const summary = summarizeThinking(value);
    expect(summary.slice(0, -5)).toBe(Array.from(value).slice(0, 100).join(""));
    expect(summary.endsWith(".....")).toBe(true);
  });
});
