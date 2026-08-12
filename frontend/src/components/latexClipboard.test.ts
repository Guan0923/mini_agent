import { afterEach, describe, expect, it, vi } from "vitest";
import { renderMarkdown } from "./MarkdownContent";
import { copySelectionWithMarkdown, selectFormula } from "./latexClipboard";

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

describe("Markdown-aware transcript copying", () => {
  it("copies the complete formula when only part of it is selected", () => {
    const root = rootFor("结果是 $x^2$。\n\n$$\\frac{a}{b}$$");
    const formula = root.querySelector<HTMLElement>("[data-latex-source]")!;
    const text = formula.firstChild!;
    select(root, text, 1, text, 3);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("copies the complete source from a MathJax-generated nested selection", () => {
    const root = document.createElement("div");
    root.innerHTML =
      '<p>前文 <span data-latex-source="$x^2$"><math xmlns="http://www.w3.org/1998/Math/MathML"><msup><mi>x</mi><mn>2</mn></msup></math></span> 后文</p>';
    document.body.appendChild(root);
    const text = root.querySelector("mi")?.firstChild;
    expect(text).not.toBeNull();
    select(root, text!, 0, text!, 1);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("keeps selected neighbor text while completing a partially selected formula", () => {
    const root = document.createElement("div");
    root.innerHTML =
      '<p>前文 <span data-latex-source="$x^2$"><math xmlns="http://www.w3.org/1998/Math/MathML"><msup><mi>x</mi><mn>2</mn></msup></math></span> 后文</p>';
    document.body.appendChild(root);
    const paragraph = root.querySelector("p")!;
    const before = paragraph.firstChild!;
    const formulaText = root.querySelector("mi")?.firstChild!;
    select(root, before, 1, formulaText, 1);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "文 $x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("supports selecting from a formula into the following text", () => {
    const root = document.createElement("div");
    root.innerHTML =
      '<p>前文 <span data-latex-source="$x^2$"><math xmlns="http://www.w3.org/1998/Math/MathML"><msup><mi>x</mi><mn>2</mn></msup></math></span> 后文</p>';
    document.body.appendChild(root);
    const formulaText = root.querySelector("mi")?.firstChild!;
    const after = root.querySelector("p")!.lastChild!;
    select(root, formulaText, 0, after, 2);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$ 后");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("preserves ordinary text while expanding every touched formula", () => {
    const root = rootFor("前文 $x^2$ 后文 $$\\frac{a}{b}$$ 结束");
    const start = root.querySelector("p")!.firstChild!;
    const end = root.lastChild!;
    select(root, start, 0, end, end.textContent?.length ?? 0);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith(
      "text/plain",
      "前文 $x^2$ 后文\n\n$$\\frac{a}{b}$$\n结束",
    );
  });

  it("keeps ordinary selections as plain Markdown text", () => {
    const root = rootFor("普通文本\n\n`$x^2$`");
    const text = root.querySelector("p")!.firstChild!;
    select(root, text, 0, text, text.textContent?.length ?? 0);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(result.setData).toHaveBeenCalledWith("text/plain", "普通文本");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });

  it("does not hijack a selection that crosses into another message", () => {
    const first = rootFor("第一条 $x$");
    const second = rootFor("第二条 $y$");
    const firstText = first.querySelector("p")!.firstChild!;
    const secondText = second.querySelector("p")!.firstChild!;
    select(first, firstText, 0, secondText, secondText.textContent?.length ?? 0);
    const result = clipboardEvent(first);

    copySelectionWithMarkdown(result.event, first);

    expect(result.setData).not.toHaveBeenCalled();
    expect(result.preventDefault).not.toHaveBeenCalled();
  });

  it("recreates Markdown delimiters for formatting and GFM structures", () => {
    const root = rootFor("**加粗** 和 [链接](https://example.com)\n\n- 第一项\n- 第二项");
    const strongText = root.querySelector("strong")!.firstChild!;
    select(root, strongText, 0, strongText, strongText.textContent?.length ?? 0);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);
    expect(result.setData).toHaveBeenCalledWith("text/plain", "**加粗**");

    const list = root.querySelector("ul")!;
    select(root, list, 0, list, list.childNodes.length);
    const listResult = clipboardEvent(root);
    copySelectionWithMarkdown(listResult.event, root);
    expect(listResult.setData).toHaveBeenCalledWith("text/plain", "-   第一项\n-   第二项");
  });

  it("selects a MathJax formula on click so its source can be copied", () => {
    const root = rootFor("结果是 $x^2$");
    const formula = root.querySelector<HTMLElement>("[data-latex-source]")!;
    selectFormula(formula, root);
    const result = clipboardEvent(root);

    copySelectionWithMarkdown(result.event, root);

    expect(document.activeElement).toBe(root);
    expect(result.setData).toHaveBeenCalledWith("text/plain", "$x^2$");
    expect(result.preventDefault).toHaveBeenCalledOnce();
  });
});
