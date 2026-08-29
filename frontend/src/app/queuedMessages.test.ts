import { describe, expect, it } from "vitest";

import {
  QUEUED_MESSAGES_STORAGE_KEY,
  loadQueuedMessages,
  mergeQueuedMessages,
  saveQueuedMessages,
} from "./queuedMessages";

describe("queued message persistence and merging", () => {
  it("round-trips queues per user and rejects invalid stored values", () => {
    const storage = localStorage;
    storage.clear();
    const queues = new Map([
      ["conversation-1", [
        { id: "one", content: "第一条", references: [{ source: "project" as const, path: "README.md" }] },
      ]],
    ]);
    saveQueuedMessages(storage, "user-1", queues);

    expect(loadQueuedMessages(storage, "user-1")).toEqual(queues);
    expect(loadQueuedMessages(storage, "user-2")).toEqual(new Map());

    storage.setItem(`${QUEUED_MESSAGES_STORAGE_KEY}:user-1`, '{"conversation-1":[{"id":1}]}');
    expect(loadQueuedMessages(storage, "user-1")).toEqual(new Map());
  });

  it("joins FIFO text with blank lines and deduplicates references in first-seen order", () => {
    expect(mergeQueuedMessages([
      {
        id: "one",
        content: "第一条",
        references: [
          { source: "project", path: "README.md" },
          { source: "upload", path: "a.txt" },
        ],
      },
      {
        id: "two",
        content: "第二条",
        references: [
          { source: "project", path: "README.md" },
          { source: "project", path: "docs/spec.md" },
        ],
      },
    ])).toEqual({
      content: "第一条\n\n第二条",
      references: [
        { source: "project", path: "README.md" },
        { source: "upload", path: "a.txt" },
        { source: "project", path: "docs/spec.md" },
      ],
    });
  });
});
