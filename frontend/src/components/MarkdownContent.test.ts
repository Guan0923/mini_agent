import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./MarkdownContent";

describe("shared Markdown and LaTeX renderer", () => {
  it("keeps all supported delimiters as complete MathJax source wrappers", () => {
    const html = renderMarkdown(
      "**bold**\n\nInline $x^2$ and \\(\\alpha+1\\).\n\n$$\\frac{a}{b}$$\n\n\\[y_1\\]",
    );
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain('data-latex-source="$x^2$"');
    expect(html).toContain('data-latex-source="\\(\\alpha+1\\)"');
    expect(html).toContain('data-latex-source="$$\\frac{a}{b}$$"');
    expect(html).toContain('data-latex-source="\\[y_1\\]"');
    expect(html.match(/math-source/g)?.length).toBe(4);
  });

  it("supports starred begin/end environments and nested matrix syntax", () => {
    const source = String.raw`\begin{align*}
  A &= \begin{pmatrix}a & b\\ c & d\end{pmatrix}\\
  B &= \ce{H2O}
\end{align*}`;
    const html = renderMarkdown(source);
    const document = new DOMParser().parseFromString(html, "text/html");
    expect(document.querySelector("[data-latex-source]")?.getAttribute("data-latex-source")).toBe(source);
    expect(document.querySelector(".math-display")).not.toBeNull();
  });

  it("keeps currency, code, incomplete math, and unsafe HTML safe", () => {
    const html = renderMarkdown(
      "Cost $12.50 and \\$5; code: `$z_2$`; pending $x\n\n<script>alert(1)</script> [bad](javascript:alert(1))",
    );
    expect(html).toContain("Cost $12.50 and $5");
    expect(html).toContain("<code>$z_2$</code>");
    expect(html).toContain("pending $x");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("onerror=");
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('data-latex-source="$12.50');
  });

  it("adds safe attributes to external links", () => {
    const html = renderMarkdown("[docs](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});
