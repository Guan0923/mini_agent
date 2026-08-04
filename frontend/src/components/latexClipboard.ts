const MATH_SOURCE_SELECTOR = "[data-latex-source]";
const BLOCK_TAGS = new Set([
  "ADDRESS",
  "ARTICLE",
  "ASIDE",
  "BLOCKQUOTE",
  "DIV",
  "DL",
  "DT",
  "DD",
  "FIGCAPTION",
  "FIGURE",
  "FOOTER",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "HEADER",
  "HR",
  "LI",
  "MAIN",
  "NAV",
  "OL",
  "P",
  "PRE",
  "SECTION",
  "TABLE",
  "TBODY",
  "TD",
  "TFOOT",
  "TH",
  "THEAD",
  "TR",
  "UL",
]);

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

function textFromNode(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue ?? "";
  if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) {
    return "";
  }

  const element = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : null;
  if (element?.hasAttribute("data-latex-source")) {
    return element.getAttribute("data-latex-source") ?? "";
  }
  if (element?.tagName === "BR") return "\n";
  if (element?.tagName === "PRE") return element.textContent ?? "";
  if (element?.tagName === "TD" || element?.tagName === "TH") {
    return Array.from(node.childNodes, textFromNode).join("") + "\t";
  }
  if (element?.tagName === "TR") {
    return Array.from(node.childNodes, textFromNode).join("").replace(/\t$/, "") + "\n";
  }

  const content = Array.from(node.childNodes, textFromNode).join("");
  return BLOCK_TAGS.has(element?.tagName ?? "") ? `\n${content}\n` : content;
}

export function extractSelectedText(fragment: DocumentFragment): string {
  return textFromNode(fragment)
    .replace(/[ \t]*\n[ \t]*/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^\n+|\n+$/g, "");
}

function replaceFormulaNodes(fragment: DocumentFragment): void {
  for (const formula of Array.from(fragment.querySelectorAll(MATH_SOURCE_SELECTOR))) {
    const source = formula.getAttribute("data-latex-source") ?? "";
    formula.replaceWith(document.createTextNode(source));
  }
}

function selectionTouchesRoot(selection: Selection, root: HTMLElement): boolean {
  return root.contains(selection.anchorNode) || root.contains(selection.focusNode);
}

export function copySelectionWithLatex(event: ClipboardEvent, root: HTMLElement): void {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return;
  if (!selectionTouchesRoot(selection, root)) return;

  const range = selection.getRangeAt(0).cloneRange();
  const startFormula = enclosingFormula(range.startContainer, root);
  const endFormula = enclosingFormula(range.endContainer, root);
  if (startFormula) range.setStartBefore(startFormula);
  if (endFormula) range.setEndAfter(endFormula);

  const fragment = range.cloneContents();
  if (!fragment.querySelector(MATH_SOURCE_SELECTOR)) return;
  replaceFormulaNodes(fragment);

  const clipboard = event.clipboardData;
  if (!clipboard) return;
  clipboard.setData("text/plain", extractSelectedText(fragment));
  event.preventDefault();
}
