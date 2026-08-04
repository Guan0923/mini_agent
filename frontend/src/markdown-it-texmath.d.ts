declare module "markdown-it-texmath" {
  import type MarkdownIt from "markdown-it";

  interface TexmathOptions {
    engine?: Pick<typeof import("katex"), "renderToString">;
    delimiters?: string | string[];
    outerSpace?: boolean;
    katexOptions?: Record<string, unknown>;
  }

  const texmath: (markdown: MarkdownIt, options?: TexmathOptions) => void;
  export default texmath;
}
