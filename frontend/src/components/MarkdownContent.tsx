import MarkdownIt from "markdown-it";
import texmath from "markdown-it-texmath";
import katex from "katex";
import "katex/dist/katex.min.css";
import "markdown-it-texmath/css/texmath.css";

const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
}).use(texmath, {
  engine: katex,
  delimiters: ["dollars", "brackets"],
  outerSpace: true,
  katexOptions: {
    throwOnError: false,
    trust: false,
    strict: "warn",
  },
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

export function renderMarkdown(text: string): string {
  return markdown.render(text || "");
}

export default function MarkdownContent({ text, className = "" }: { text: string; className?: string }) {
  const classes = ["markdown", className].filter(Boolean).join(" ");
  return <div className={classes} dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />;
}
