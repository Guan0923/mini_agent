import { beforeEach, describe, expect, it } from "vitest";
import { loadConversations } from "./App";

beforeEach(() => localStorage.clear());

describe("conversation recovery", () => {
  it("does not restore a stale running animation from localStorage", () => {
    localStorage.setItem(
      "mini-agent-conversations",
      JSON.stringify([
        {
          id: "old-run",
          title: "旧任务",
          messages: [
            { id: "assistant-1", role: "assistant", content: "", events: [], running: true },
          ],
        },
      ]),
    );

    const conversations = loadConversations("mini-agent-conversations");
    expect(conversations[0]?.messages[0]).toMatchObject({
      running: false,
      status: "上次运行已中断",
    });
  });
});
