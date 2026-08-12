import type { MathJaxBrowserConfig, MathJaxBrowserInstance } from "./mathjax.d";

const MATHJAX_SCRIPT_ATTRIBUTE = "data-mini-agent-mathjax";
const MATHJAX_SOURCE = "/mathjax/tex-svg.js";
export const MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML";

export const TEX_EXTENSIONS = [
  "ams",
  "amscd",
  "autoload",
  "bbm",
  "boldsymbol",
  "braket",
  "bussproofs",
  "cancel",
  "cases",
  "centernot",
  "color",
  "empheq",
  "extpfeil",
  "gensymb",
  "mathtools",
  "mhchem",
  "newcommand",
  "physics",
  "tagformat",
  "textcomp",
  "unicode",
  "units",
  "upgreek",
];

let mathJaxPromise: Promise<MathJaxBrowserInstance> | null = null;

/**
 * MathML is the only rendering path that gives the browser real formula text
 * nodes, which allows native partial selection.  Keep the check deliberately
 * conservative so unsupported browsers use the existing SVG fallback.
 */
export function supportsNativeMathML(): boolean {
  if (typeof document === "undefined" || typeof window === "undefined") return false;
  const mathElement = document.createElementNS(MATHML_NAMESPACE, "math");
  const browserMathMLElement = (window as Window & { MathMLElement?: unknown }).MathMLElement;
  // Chromium supports native MathML without exposing a global MathMLElement
  // constructor.  CSS.supports is the interoperable feature probe; retain the
  // constructor check as a jsdom/older-browser fallback for our tests and SVG
  // fallback path.
  const cssMathML =
    typeof CSS !== "undefined" &&
    typeof CSS.supports === "function" &&
    CSS.supports("math-style", "normal");
  return (
    mathElement.namespaceURI === MATHML_NAMESPACE &&
    (cssMathML || typeof browserMathMLElement === "function")
  );
}

export function mathJaxConfig(): MathJaxBrowserConfig {
  return {
    loader: {
      load: [...TEX_EXTENSIONS.map((extension) => `[tex]/${extension}`), "ui/safe"],
    },
    // MarkdownContent owns the conversion lifecycle.  Prevent MathJax's
    // browser startup hook from eagerly replacing the safe source wrappers
    // with SVG before the native MathML transaction has completed.
    startup: { typeset: false },
    tex: {
      packages: { "[+]": TEX_EXTENSIONS },
      inlineMath: [
        ["\\(", "\\)"],
        ["$", "$"],
      ],
      displayMath: [
        ["\\[", "\\]"],
        ["$$", "$$"],
      ],
      processEscapes: true,
      processEnvironments: true,
    },
    options: {
      enableMenu: false,
      enableExplorer: false,
      enableExplorerHelp: false,
    },
    svg: {
      fontCache: "global",
    },
  };
}

function getLoadedMathJax(): MathJaxBrowserInstance | null {
  const candidate = window.MathJax;
  if (!candidate?.typesetPromise || !candidate?.typesetClear) return null;
  return candidate;
}

export function loadMathJax(): Promise<MathJaxBrowserInstance> {
  if (mathJaxPromise) return mathJaxPromise;

  mathJaxPromise = new Promise<MathJaxBrowserInstance>((resolve, reject) => {
    const loaded = getLoadedMathJax();
    if (loaded) {
      resolve(loaded);
      return;
    }

    window.MathJax = {
      ...mathJaxConfig(),
      ...window.MathJax,
    };

    const existing = document.querySelector<HTMLScriptElement>(
      `script[${MATHJAX_SCRIPT_ATTRIBUTE}]`,
    );
    const script = existing ?? document.createElement("script");
    if (!existing) {
      script.src = MATHJAX_SOURCE;
      script.async = true;
      script.setAttribute(MATHJAX_SCRIPT_ATTRIBUTE, "true");
      document.head.appendChild(script);
    }

    let finishStarted = false;
    const finish = () => {
      if (finishStarted) return;
      finishStarted = true;
      // MathJax 4 fires the script load event before it attaches the public
      // typeset/tex2mml methods.  Wait for startup first, then validate the
      // fully initialized instance.
      const candidate = window.MathJax;
      const startup = candidate?.startup?.promise ?? Promise.resolve();
      startup.then(() => {
        const instance = getLoadedMathJax();
        if (!instance) throw new Error("MathJax 未提供浏览器排版接口");
        resolve(instance);
      }).catch((error: unknown) => {
        mathJaxPromise = null;
        reject(error);
      });
    };

    script.addEventListener("load", finish, { once: true });
    // An existing script can already be complete (for example after a hot
    // reload), in which case no future load event will be emitted.
    const readyState = (script as HTMLScriptElement & { readyState?: string }).readyState;
    if (existing && (readyState === "complete" || getLoadedMathJax())) finish();
    script.addEventListener(
      "error",
      () => {
        mathJaxPromise = null;
        reject(new Error("MathJax 资源加载失败"));
      },
      { once: true },
    );
  });

  return mathJaxPromise;
}

export function resetMathJaxLoaderForTests(): void {
  mathJaxPromise = null;
}
