import type { ChatMode } from "./types";

export const MODE_STORAGE_KEY = "mini-agent-session-modes";

export interface SimpleStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function loadSessionModes(storage: SimpleStorage): Record<string, ChatMode> {
  try {
    const raw = storage.getItem(MODE_STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, ChatMode>) : {};
  } catch {
    return {};
  }
}

export function saveSessionModes(storage: SimpleStorage, modes: Record<string, ChatMode>): void {
  storage.setItem(MODE_STORAGE_KEY, JSON.stringify(modes));
}
