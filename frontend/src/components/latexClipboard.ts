import TurndownService from "turndown";
import { gfm } from "turndown-plugin-gfm";

export const MATH_SOURCE_SELECTOR = "[data-latex-source]";

const markdownConverter = new TurndownService({
  headingStyle: "atx",
  bulletListMarker: "-",
  codeBlockStyle: "fenced",
  fence: "```",
  emDelimiter: "*",
  strongDelimiter: "**",
  linkStyle: "inlined",
});
markdownConverter.use(gfm);
markdownConverter.addRule("latexSource", {
  filter: (node) => node.hasAttribute("data-latex-source"),
  replacement: (_content, node) => {
    const source = node.getAttribute("data-latex-source") ?? "";
    return node.classList.contains("math-display") ? `\n${source}\n` : source;
  },
});

function elementForNode(node: Node | null): Element | null {
  if (!node) return null;
  if (node.nodeType === Node.ELEMENT_NODE) return node as Element;
  return node.parentElement;
}

function enclosingFormula(node: Node | null, root: HTMLElement): Element | null {
  const element = elementForNode(node);
  const formula = element?.closest(MATH_SOURCE_SELECTOR);
  return formula && root.contains(formula) ? formula : null;
}

function selectionTouchesRoot(selection: Selection, root: HTMLElement): boolean {
  // Only rewrite copies whose complete selection belongs to this message.
  // Using an OR here would let a message hijack a cross-message selection
  // merely because one endpoint happened to be inside its DOM subtree.
  return root.contains(selection.anchorNode) && root.contains(selection.focusNode);
}

function rangeForNode(node: Node): Range {
  const range = document.createRange();
  range.selectNode(node);
  return range;
}

function rangeOverlapsNode(range: Range, node: Node): boolean {
  const nodeRange = rangeForNode(node);
  return (
    range.compareBoundaryPoints(Range.END_TO_START, nodeRange) > 0 &&
    range.compareBoundaryPoints(Range.START_TO_END, nodeRange) < 0
  );
}

function enclosingSyntaxElement(node: Node | null, root: HTMLElement): Element | null {
  const element = elementForNode(node);
  if (!element || element === root || !root.contains(element)) return null;
  if (element.closest(MATH_SOURCE_SELECTOR)) return null;
  return ["A", "CODE", "DEL", "EM", "S", "STRONG"].includes(element.tagName)
    ? element
    : null;
}

function expandRangeToStableSourceUnits(range: Range, root: HTMLElement): void {
  const touchedFormulas = Array.from(root.querySelectorAll(MATH_SOURCE_SELECTOR)).filter((formula) =>
    rangeOverlapsNode(range, formula),
  );
  const startFormula = enclosingFormula(range.startContainer, root);
  const endFormula = enclosingFormula(range.endContainer, root);
  const touchedFormula = touchedFormulas.length > 0;

  // Native MathML can place the boundary inside an <mi>, <mn>, <mfrac>, etc.
  // Expand only those boundary formulas; formulas wholly inside the range are
  // already cloned as complete wrapper elements.
  if (startFormula) range.setStartBefore(startFormula);
  if (endFormula) range.setEndAfter(endFormula);

  // Some browsers expose an element itself as the boundary container.  The
  // overlap pass keeps those cases consistent with text-node boundaries.
  for (const formula of touchedFormulas) {
    const formulaRange = rangeForNode(formula);
    if (range.compareBoundaryPoints(Range.START_TO_START, formulaRange) > 0) {
      range.setStartBefore(formula);
    }
    if (range.compareBoundaryPoints(Range.END_TO_END, formulaRange) < 0) {
      range.setEndAfter(formula);
    }
  }

  // A selection wholly inside one formatting element would otherwise clone
  // only its text node and lose the delimiters (for example, **bold**).
  const startSyntax = enclosingSyntaxElement(range.startContainer, root);
  const endSyntax = enclosingSyntaxElement(range.endContainer, root);
  if (!touchedFormula && startSyntax && startSyntax === endSyntax) {
    range.selectNode(startSyntax);
  }
}

function markdownFromFragment(fragment: DocumentFragment): string {
  return markdownConverter.turndown(fragment).trim();
}

/** Convert a selected, rendered transcript fragment back into Markdown. */
export function extractSelectedText(fragment: DocumentFragment): string {
  return markdownFromFragment(fragment);
}

/** Select a complete formula so SVG glyphs can be copied as their source. */
export function selectFormula(formula: Element, root: HTMLElement): void {
  if (!root.contains(formula)) return;
  const selection = window.getSelection();
  if (!selection) return;
  if (!root.hasAttribute("tabindex")) root.tabIndex = -1;
  root.focus({ preventScroll: true });
  const range = document.createRange();
  range.selectNode(formula);
  selection.removeAllRanges();
  selection.addRange(range);
}

/** Replace the browser's visible-text copy with Markdown/LaTeX source. */
export function copySelectionWithMarkdown(event: ClipboardEvent, root: HTMLElement): void {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return;
  if (!selectionTouchesRoot(selection, root)) return;

  const range = selection.getRangeAt(0).cloneRange();
  expandRangeToStableSourceUnits(range, root);
  const fragment = range.cloneContents();
  const markdown = markdownFromFragment(fragment);
  if (!markdown) return;

  const clipboard = event.clipboardData;
  if (!clipboard) return;
  clipboard.setData("text/plain", markdown);
  event.preventDefault();
}

// Keep the old name as a compatibility seam for callers and existing clients.
export const copySelectionWithLatex = copySelectionWithMarkdown;
