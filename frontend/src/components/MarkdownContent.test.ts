import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./MarkdownContent";

describe("shared Markdown and LaTeX renderer", () => {
  it("renders GFM-style Markdown and all TUI math delimiters", () => {
    const html = renderMarkdown(
      "**bold**\n\nInline $x^2$ and \\(\\alpha+1\\).\n\n$$\\frac{a}{b}$$\n\n\\[y_1\\]",
    );
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("katex");
    expect((html.match(/katex/g) ?? []).length).toBeGreaterThanOrEqual(4);
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
  });

  it("adds safe attributes to external links", () => {
    const html = renderMarkdown("[docs](https://example.com)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});
