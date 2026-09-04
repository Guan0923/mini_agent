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
          { source: "project", path: "C:/workspace/README.md", display_path: "README.md" },
          { source: "upload", path: "C:/uploads/a.txt", display_path: "a.txt" },
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
          { source: "project", path: "C:/workspace/README.md", display_path: "README.md" },
          { source: "project", path: "C:/workspace/docs/spec.md", display_path: "docs/spec.md" },
        ],
      },
    ])).toEqual({
      content: "第一条\n\n第二条",
      references: [
        { source: "project", path: "C:/workspace/README.md", display_path: "README.md" },
        { source: "upload", path: "C:/uploads/a.txt", display_path: "a.txt" },
        { source: "project", path: "C:/workspace/docs/spec.md", display_path: "docs/spec.md" },
      ],
    });
  });
});
