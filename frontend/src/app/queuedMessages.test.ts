import { describe, expect, it } from "vitest";

import { mergeQueuedMessages } from "./queuedMessages";

describe("queued message merging", () => {
  it("joins FIFO text with blank lines and deduplicates references in first-seen order", () => {
    expect(mergeQueuedMessages([
      {
        id: "one",
        thread_id: "thread",
        content: "第一条",
        state: "pending",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
        references: [
          { source: "project", path: "README.md" },
          { source: "upload", path: "a.txt" },
        ],
      },
      {
        id: "two",
        thread_id: "thread",
        content: "第二条",
        state: "pending",
        created_at: "2026-01-01T00:00:01Z",
        updated_at: "2026-01-01T00:00:01Z",
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
