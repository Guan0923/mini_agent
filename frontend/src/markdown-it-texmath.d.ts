declare module "markdown-it-texmath" {
  import type MarkdownIt from "markdown-it";

  interface TexmathEngine {
    renderToString: (source: string, options?: Record<string, unknown>) => string;
  }

  interface TexmathOptions {
    engine?: TexmathEngine;
    delimiters?: string | string[];
    outerSpace?: boolean;
    katexOptions?: Record<string, unknown>;
  }

  const texmath: (markdown: MarkdownIt, options?: TexmathOptions) => void;
  export default texmath;
}
