import type { FileReference } from "../types";
import type { QueuedMessage } from "./types";

export const QUEUED_MESSAGES_STORAGE_KEY = "mini-agent-queued-messages";

function validReference(value: unknown): value is FileReference {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<FileReference>;
  return (candidate.source === "project" || candidate.source === "upload")
    && typeof candidate.path === "string"
    && candidate.path.length > 0;
}

function validQueuedMessage(value: unknown): value is QueuedMessage {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<QueuedMessage>;
  return typeof candidate.id === "string"
    && candidate.id.length > 0
    && typeof candidate.content === "string"
    && (candidate.sendingSteeringId === undefined || typeof candidate.sendingSteeringId === "string")
    && (candidate.sendingTurnId === undefined || typeof candidate.sendingTurnId === "string")
    && (candidate.references === undefined
      || (Array.isArray(candidate.references) && candidate.references.every(validReference)));
}

export function loadQueuedMessages(storage: Storage): Map<string, QueuedMessage[]> {
  try {
    const raw = storage.getItem(QUEUED_MESSAGES_STORAGE_KEY);
    if (!raw) return new Map();
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return new Map();
    return new Map(
      Object.entries(parsed)
        .filter((entry): entry is [string, QueuedMessage[]] =>
          entry[0].length > 0 && Array.isArray(entry[1]) && entry[1].every(validQueuedMessage))
        .filter(([, items]) => items.length > 0),
    );
  } catch {
    return new Map();
  }
}

export function saveQueuedMessages(
  storage: Storage,
  queues: ReadonlyMap<string, QueuedMessage[]>,
): void {
  const key = QUEUED_MESSAGES_STORAGE_KEY;
  if (queues.size === 0) {
    storage.removeItem(key);
    return;
  }
  storage.setItem(key, JSON.stringify(Object.fromEntries(queues)));
}

export function mergeQueuedMessages(items: readonly QueuedMessage[]): {
  content: string;
  references: FileReference[] | undefined;
} {
  const references: FileReference[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    for (const reference of item.references ?? []) {
      const key = `${reference.source}:${reference.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      references.push(reference);
    }
  }
  return {
    content: items.map((item) => item.content).join("\n\n"),
    references: references.length > 0 ? references : undefined,
  };
}
