import { useEffect, useMemo, useRef, type RefObject } from "react";
import MarkdownIt from "markdown-it";
import type { Token } from "markdown-it";
import texmath from "markdown-it-texmath";
import { MATHML_NAMESPACE, loadMathJax, supportsNativeMathML } from "../math";
import type { MathJaxBrowserInstance } from "../math/mathjax.d";
import { MATH_SOURCE_SELECTOR, copySelectionWithMarkdown, selectFormula } from "./latexClipboard";

const MATH_SOURCE_ATTRIBUTE = "data-latex-source";

type TexmathRule = {
  rex: RegExp;
  tag?: string;
  pre?: (source: string, outerSpace: boolean, offset: number) => boolean;
  post?: (source: string, outerSpace: boolean, offset: number) => boolean;
};

type TexmathWithRules = typeof texmath & {
  rules?: {
    dollars?: {
      inline?: TexmathRule[];
    };
    beg_end?: {
      block?: TexmathRule[];
    };
  };
};

const texmathWithRules = texmath as TexmathWithRules;
const dollarRules = texmathWithRules.rules?.dollars?.inline ?? [];
const formulaBoundary = /[\s\p{P}\p{S}]/u;
const cjkOrBoundary = (source: string, offset: number): boolean => {
  const previous = offset > 0 ? source[offset - 1] : "";
  return !previous || formulaBoundary.test(previous) || /\p{Script=Han}/u.test(previous);
};
const formulaAfterBoundary = (source: string, offset: number): boolean => {
  const next = source[offset + 1] ?? "";
  return !next || formulaBoundary.test(next);
};
for (const rule of dollarRules) {
  rule.pre = (source, _outerSpace, offset) => cjkOrBoundary(source, offset);
  rule.post = (source, _outerSpace, offset) => formulaAfterBoundary(source, offset);
}
const beginEndRule = texmathWithRules.rules?.beg_end?.block?.[0];
if (beginEndRule) {
  // The upstream rule only accepts lower-case names without a trailing '*'.
  // MathJax accepts the full family of standard environment names.
  beginEndRule.rex = /(\\begin\{([A-Za-z][A-Za-z0-9*_.:-]*)\}[\s\S]+?\\end\{\2\})/gmy;
}

const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
}).use(texmath, {
  // Rendering is supplied by MathJax after Markdown has created safe wrappers.
  engine: { renderToString: () => "" },
  delimiters: ["dollars", "brackets", "beg_end"],
  // The patched dollar boundaries permit CJK text and punctuation while still
  // rejecting decimal/currency false positives and code-like identifiers.
  outerSpace: true,
});

const defaultLinkOpen = markdown.renderer.rules.link_open;
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index];
  token.attrSet("target", "_blank");
  token.attrSet("rel", "noopener noreferrer");
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, env, self)
    : self.renderToken(tokens, index, options);
};

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function inlineClosingDelimiter(markup: string): string {
  if (markup === "\\(") return "\\)";
  return markup;
}

function blockDelimiters(token: Token): { open: string; close: string } | null {
  if (token.tag === "\\") return null;
  if (token.tag === "\\[") return { open: "\\[", close: "\\]" };
  return { open: "$$", close: "$$" };
}

type MathTokenSource = {
  source: string;
  body: string;
  display: boolean;
  equationNumber?: string;
};

function sourceForToken(token: Token): MathTokenSource {
  if (!token.block) {
    const markup = token.markup || "$";
    return {
      source: `${markup}${token.content}${inlineClosingDelimiter(markup)}`,
      body: token.content,
      display: token.type === "math_inline_double",
    };
  }

  const delimiters = blockDelimiters(token);
  if (!delimiters) return { source: token.content, body: token.content, display: true };
  const equationNumber = token.type === "math_block_eqno" && token.info ? ` (${token.info})` : "";
  return {
    source: `${delimiters.open}${token.content}${delimiters.close}${equationNumber}`,
    body: token.content,
    display: true,
    equationNumber: equationNumber || undefined,
  };
}

function renderMathToken(token: Token): string {
  const { source, body, display, equationNumber } = sourceForToken(token);
  const className = display ? "math-source math-display" : "math-source math-inline";
  const tag = display ? "div" : "span";
  const equationNumberAttribute = equationNumber
    ? ` data-equation-number="${escapeHtml(equationNumber)}"`
    : "";
  return `<${tag} class="${className}" ${MATH_SOURCE_ATTRIBUTE}="${escapeHtml(source)}" data-latex-body="${escapeHtml(body)}" data-math-display="${String(display)}"${equationNumberAttribute}>${escapeHtml(source)}</${tag}>`;
}

for (const ruleName of ["math_inline", "math_inline_double", "math_block", "math_block_eqno"]) {
  markdown.renderer.rules[ruleName] = (tokens, index) => renderMathToken(tokens[index]);
}

const typesetQueues = new WeakMap<HTMLElement, Promise<void>>();

type NativeMathReplacement = {
  formula: HTMLElement;
  mathml: Element;
};

function parseSafeMathML(markup: string): Element | null {
  const parsed = new DOMParser().parseFromString(markup, "application/xml");
  const root = parsed.documentElement;
  if (!root || root.localName !== "math" || root.namespaceURI !== MATHML_NAMESPACE) return null;
  if (parsed.querySelector("parsererror")) return null;

  const elements = [root, ...Array.from(root.querySelectorAll("*"))];
  for (const element of elements) {
    if (element.namespaceURI !== MATHML_NAMESPACE) return null;
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || name === "href" || name === "xlink:href" || name === "src") {
        element.removeAttribute(attribute.name);
      } else if (name === "style" && /url\s*\(/iu.test(attribute.value)) {
        element.removeAttribute(attribute.name);
      }
    }
  }

  return document.importNode(root, true) as Element;
}

export async function renderNativeMathML(
  root: HTMLElement,
  mathJax: MathJaxBrowserInstance,
  isCurrent: () => boolean,
): Promise<boolean> {
  if (!supportsNativeMathML() || !mathJax.tex2mmlPromise) {
    return false;
  }

  const replacements: NativeMathReplacement[] = [];
  const formulas = Array.from(root.querySelectorAll<HTMLElement>(MATH_SOURCE_SELECTOR));
  for (const formula of formulas) {
    const body = formula.getAttribute("data-latex-body");
    if (body === null || !isCurrent()) {
      return false;
    }

    let markup: string;
    try {
      markup = await mathJax.tex2mmlPromise(body, {
        display: formula.getAttribute("data-math-display") === "true",
      });
    } catch (error) {
      return false;
    }
    if (!isCurrent()) {
      return false;
    }

    const mathml = parseSafeMathML(markup);
    if (!mathml) {
      return false;
    }
    replacements.push({ formula, mathml });
  }

  if (!isCurrent()) {
    return false;
  }
  for (const { formula, mathml } of replacements) {
    formula.replaceChildren(mathml);
    const equationNumber = formula.getAttribute("data-equation-number");
    if (equationNumber) {
      const number = document.createElement("span");
      number.className = "math-equation-number";
      number.textContent = equationNumber;
      formula.append(number);
    }
    formula.classList.add("math-native");
    formula.dataset.mathRenderer = "mathml";
  }
  return true;
}

function markSvgFallback(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>(MATH_SOURCE_SELECTOR).forEach((formula) => {
    formula.classList.add("math-svg-fallback");
    formula.dataset.mathRenderer = "svg";
  });
}

function enqueueMathJaxTypesetting(
  root: HTMLElement,
  isCurrent: () => boolean,
): Promise<void> {
  const previous = typesetQueues.get(root) ?? Promise.resolve();
  const next = previous
    .catch(() => undefined)
    .then(async () => {
      if (!isCurrent()) return;
      const mathJax = await loadMathJax();
      if (!isCurrent()) return;
      mathJax.typesetClear?.([root]);
      if (!isCurrent()) return;

      if (await renderNativeMathML(root, mathJax, isCurrent)) return;
      if (!isCurrent()) return;

      await mathJax.typesetPromise?.([root]);
      if (isCurrent()) markSvgFallback(root);
    });
  typesetQueues.set(root, next);
  void next.then(
    () => {
      if (typesetQueues.get(root) === next) typesetQueues.delete(root);
    },
    () => {
      if (typesetQueues.get(root) === next) typesetQueues.delete(root);
    },
  );
  return next;
}

export function renderMarkdown(text: string): string {
  return markdown.render(text || "");
}

function useMathJaxTypesetting(rootRef: RefObject<HTMLDivElement>, html: string): void {
  const generationRef = useRef(0);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !root.querySelector("[data-latex-source]")) return;

    let cancelled = false;
    const generation = ++generationRef.current;
    let timer: number | undefined;
    const schedule = window.setTimeout(() => {
      void enqueueMathJaxTypesetting(root, () => !cancelled && generation === generationRef.current).catch(
        () => undefined,
      );
    }, 0);
    timer = schedule;

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [html, rootRef]);
}

export default function MarkdownContent({ text, className = "" }: { text: string; className?: string }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const html = useMemo(() => renderMarkdown(text), [text]);
  useMathJaxTypesetting(rootRef, html);
  const classes = ["markdown", className].filter(Boolean).join(" ");

  return (
    <div
      ref={rootRef}
      className={classes}
      tabIndex={-1}
      dangerouslySetInnerHTML={{ __html: html }}
      onClick={(event) => {
        const target = event.target as Element;
        const formula = target.closest(MATH_SOURCE_SELECTOR);
        const selection = window.getSelection();
        const svgFallback = formula?.classList.contains("math-svg-fallback") || !supportsNativeMathML();
        if (formula && svgFallback && rootRef.current?.contains(formula) && (!selection || selection.isCollapsed)) {
          selectFormula(formula, event.currentTarget);
        }
      }}
      onCopyCapture={(event) => copySelectionWithMarkdown(event.nativeEvent, event.currentTarget)}
    />
  );
}
