import { afterEach, describe, expect, it, vi } from "vitest";
import { loadMathJax, mathJaxConfig, resetMathJaxLoaderForTests, supportsNativeMathML, TEX_EXTENSIONS } from "./math";

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
    expect(config.startup?.typeset).toBe(false);
  });

  it("waits for MathJax startup before reading the public conversion methods", async () => {
    let resolveStartup!: () => void;
    const startup = new Promise<void>((resolve) => {
      resolveStartup = resolve;
    });
    const loading = loadMathJax();
    const script = document.querySelector<HTMLScriptElement>("script[data-mini-agent-mathjax]");
    expect(script).not.toBeNull();

    const instance = {
      startup: { promise: startup },
      typesetClear: vi.fn(),
      typesetPromise: vi.fn().mockResolvedValue(undefined),
      tex2mmlPromise: vi.fn().mockResolvedValue("<math />"),
    };
    window.MathJax = instance;
    script!.dispatchEvent(new Event("load"));
    resolveStartup();

    await expect(loading).resolves.toBe(instance);
  });

  it("falls back when the browser does not advertise native MathML", () => {
    expect(supportsNativeMathML()).toBe(false);
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
