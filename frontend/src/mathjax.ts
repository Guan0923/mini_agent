import type { MathJaxBrowserConfig, MathJaxBrowserInstance } from "./mathjax.d";

const MATHJAX_SCRIPT_ATTRIBUTE = "data-mini-agent-mathjax";
const MATHJAX_SOURCE = "/mathjax/tex-svg.js";

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

export function mathJaxConfig(): MathJaxBrowserConfig {
  return {
    loader: {
      load: [...TEX_EXTENSIONS.map((extension) => `[tex]/${extension}`), "ui/safe"],
    },
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

    const finish = () => {
      const instance = getLoadedMathJax();
      if (!instance) {
        mathJaxPromise = null;
        reject(new Error("MathJax 未提供浏览器排版接口"));
        return;
      }
      const startup = instance.startup?.promise ?? Promise.resolve();
      startup.then(() => resolve(instance)).catch((error: unknown) => {
        mathJaxPromise = null;
        reject(error);
      });
    };

    script.addEventListener("load", finish, { once: true });
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
