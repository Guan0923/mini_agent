export {};

declare global {
  interface Window {
    MathJax?: MathJaxBrowserInstance & MathJaxBrowserConfig;
  }
}

export interface MathJaxBrowserInstance {
  startup?: {
    promise?: Promise<void>;
  };
  typesetClear?: (elements?: Element[]) => void;
  typesetPromise?: (elements?: Element[]) => Promise<void>;
  tex2mmlPromise?: (source: string, options?: Record<string, unknown>) => Promise<string>;
}

export interface MathJaxBrowserConfig {
  loader?: {
    load?: string[];
  };
  startup?: Record<string, unknown>;
  tex?: Record<string, unknown>;
  options?: Record<string, unknown>;
  svg?: Record<string, unknown>;
}
