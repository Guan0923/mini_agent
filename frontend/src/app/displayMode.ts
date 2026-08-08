import type { DisplayMode } from "../types";

export function effectiveDisplayMode(
  mode: DisplayMode,
  development = import.meta.env.DEV,
): DisplayMode {
  return mode === "developer" && !development ? "verbose" : mode;
}
