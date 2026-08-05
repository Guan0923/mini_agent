import { afterEach, describe, expect, it, vi } from "vitest";
import { renderMarkdown } from "./MarkdownContent";
import { copySelectionWithLatex } from "./latexClipboard";

function clipboardEvent(root: HTMLElement) {
  const setData = vi.fn();
  const preventDefault = vi.fn();
  const event = {
    clipboardData: { setData },
    preventDefault,
  } as unknown as ClipboardEvent;
  return { event, setData, preventDefault, root };
}

function select(root: HTMLElement, start: Node, startOffset: number, end: Node, endOffset: number) {
  const range = document.createRange();
  range.setStart(start, startOffset);
  range.setEnd(end, endOffset);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
}

function rootFor(markdown: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = renderMarkdown(markdown);
  document.body.appendChild(root);
  return root;
}

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  document.body.replaceChildren();
});

describe("LaTeX-aware transcript copying", () => {
  it("copies the complete formula when only part of it is selected", () => {
    const root = rootFor("结果是 $x^2$。\n\n$$\\frac{a}{b}$$");
    const formula = root.querySelector<HTMLElement>("[data-latex-source]")!;
    const text = formula.firstChild!;
    select(root, text, 1, text, 3);
    const result = clipboardEvent(root);

    copySelectionWithLatex(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("copies the complete source from a MathJax-generated nested selection", () => {
    const root = document.createElement("div");
    root.innerHTML =
      '<p>前文 <span data-latex-source="$x^2$"><mjx-container><svg><g><text>x^2</text></g></svg></mjx-container></span> 后文</p>';
    document.body.appendChild(root);
    const text = root.querySelector("svg text")?.firstChild;
    expect(text).not.toBeNull();
    select(root, text!, 1, text!, 2);
    const result = clipboardEvent(root);

    copySelectionWithLatex(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });
  it("preserves ordinary text while expanding every touched formula", () => {
    const root = rootFor("前文 $x^2$ 后文 $$\\frac{a}{b}$$ 结束");
    const start = root.querySelector("p")!.firstChild!;
    const end = root.lastChild!;
    select(root, start, 0, end, end.textContent?.length ?? 0);
    const result = clipboardEvent(root);

    copySelectionWithLatex(result.event, root);

    expect(result.setData).toHaveBeenCalledWith(
      "text/plain",
      "前文 $x^2$ 后文\n$$\\frac{a}{b}$$ 结束",
    );
  });

  it("leaves ordinary and code selections to native browser copying", () => {
    const root = rootFor("普通文本\n\n`$x^2$`");
    const text = root.querySelector("p")!.firstChild!;
    select(root, text, 0, text, text.textContent?.length ?? 0);
    const result = clipboardEvent(root);

    copySelectionWithLatex(result.event, root);

    expect(result.setData).not.toHaveBeenCalled();
    expect(result.preventDefault).not.toHaveBeenCalled();
  });
});
