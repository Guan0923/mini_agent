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
}

export interface MathJaxBrowserConfig {
  loader?: {
    load?: string[];
  };
  tex?: Record<string, unknown>;
  options?: Record<string, unknown>;
  svg?: Record<string, unknown>;
}
