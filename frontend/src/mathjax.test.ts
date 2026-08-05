import { afterEach, describe, expect, it, vi } from "vitest";
import { loadMathJax, mathJaxConfig, resetMathJaxLoaderForTests, TEX_EXTENSIONS } from "./mathjax";

afterEach(() => {
  resetMathJaxLoaderForTests();
  window.MathJax = undefined;
  document.querySelectorAll("script[data-mini-agent-mathjax]").forEach((script) => script.remove());
});

describe("MathJax browser integration", () => {
  it("configures the broad TeX extension set and safe SVG output", () => {
    const config = mathJaxConfig();
    expect(TEX_EXTENSIONS).toEqual(expect.arrayContaining(["ams", "mathtools", "mhchem", "physics", "cancel", "units"]));
    expect(config.loader?.load).toContain("[tex]/mhchem");
    expect(config.loader?.load).toContain("ui/safe");
    expect(config.options?.enableMenu).toBe(false);
    expect(config.options?.enableExplorer).toBe(false);
    expect(config.options?.enableExplorerHelp).toBe(false);
    expect(config.tex?.processEnvironments).toBe(true);
    expect(config.svg?.fontCache).toBe("global");
  });

  it("reuses an already loaded MathJax instance", async () => {
    const instance = {
      typesetClear: vi.fn(),
      typesetPromise: vi.fn().mockResolvedValue(undefined),
    };
    window.MathJax = instance;

    const first = loadMathJax();
    const second = loadMathJax();

    expect(second).toBe(first);
    await expect(first).resolves.toBe(instance);
    expect(document.querySelector("script[data-mini-agent-mathjax]")).toBeNull();
  });
});
