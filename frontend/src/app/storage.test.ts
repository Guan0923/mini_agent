import { describe, expect, it } from "vitest";

import type { SessionInfo } from "../api";
import type { Conversation } from "../types";
import { mergeConversationSummaries, summaryToConversation } from "./storage";

function summary(
  threadId: string,
  messageCount: number,
  conversationUpdatedAt: string,
  deletedAt?: string,
): SessionInfo {
  return {
    session_id: "session-1",
    thread_id: threadId,
    title: threadId,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    conversation_updated_at: conversationUpdatedAt,
    message_count: messageCount,
    last_run_status: null,
    deleted_at: deletedAt,
  };
}

describe("Sidebar summary projection", () => {
  it("uses the backend message count and conversation activity time", () => {
    expect(summaryToConversation(summary("thread-1", 7, "2026-01-03T00:00:00Z"))).toMatchObject({
      id: "thread-1",
      messageCount: 7,
      updatedAt: "2026-01-03T00:00:00Z",
    });
  });

  it("keeps loaded runtime state while following backend order and deletion state", () => {
    const previous: Conversation[] = [{
      id: "thread-1",
      threadId: "thread-1",
      sessionId: "session-1",
      title: "old",
      messages: [{ id: "message-1", role: "user", content: "loaded", events: [] }],
      messagesLoaded: true,
      activeTurnId: "turn-1",
      messageCount: 1,
      updatedAt: "2026-01-01T00:00:00Z",
    }];
    const merged = mergeConversationSummaries(previous, [
      summary("thread-2", 4, "2026-01-04T00:00:00Z"),
      summary("thread-1", 3, "2026-01-03T00:00:00Z"),
      summary("deleted", 2, "2026-01-05T00:00:00Z", "2026-01-05T00:00:00Z"),
    ]);

    expect(merged.map((conversation) => conversation.id)).toEqual(["thread-2", "thread-1"]);
    expect(merged[1]).toMatchObject({
      title: "thread-1",
      messageCount: 3,
      messagesLoaded: true,
      activeTurnId: "turn-1",
      messages: [{ content: "loaded" }],
    });
  });

  it("does not let an older poll lower optimistic activity for a running conversation", () => {
    const previous: Conversation[] = [{
      id: "thread-1",
      threadId: "thread-1",
      sessionId: "session-1",
      title: "running",
      messages: [],
      messageCount: 4,
      updatedAt: "2026-01-04T00:00:00Z",
    }];
    const stale = summary("thread-1", 2, "2026-01-03T00:00:00Z");

    expect(mergeConversationSummaries(previous, [stale], new Set(["thread-1"]))[0]).toMatchObject({
      messageCount: 4,
      updatedAt: "2026-01-04T00:00:00Z",
    });
    expect(mergeConversationSummaries(previous, [stale])[0]).toMatchObject({
      messageCount: 2,
      updatedAt: "2026-01-03T00:00:00Z",
    });
  });
});
