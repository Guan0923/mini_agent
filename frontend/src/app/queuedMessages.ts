import type { QueuedMessage } from "./types";

export function mergeQueuedMessages(items: readonly QueuedMessage[]): {
  content: string;
  references: import("../types").FileReference[] | undefined;
} {
  const references: import("../types").FileReference[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    for (const reference of item.references) {
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
