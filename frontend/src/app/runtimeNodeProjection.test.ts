import { describe, expect, it } from "vitest";
import { assistantContentFromRuntimeNode, isAssistantMessageNode } from "./runtimeNodeProjection";
import type { RuntimeStateNode } from "../types";

function node(data: RuntimeStateNode["data"]): RuntimeStateNode {
  return {
    session_id: "s",
    parent_session_id: "s",
    id: "n",
    parent_id: "p",
    version: "0.3.0",
    firstKeptEntryId: "n",
    compactionIdx: "n",
    user: "",
    provider_name: "",
    model: { reasoning_effort: "medium", current_model: "demo", context_length: 128000, output_length: 8192, thinking: "enable", temperature: 1 },
    permission_mode: "approval_for_me",
    running_mode: "agent",
    usage: { input_tokens: null, cached_tokens: null, output_tokens: null, reasoning_tokens: null, total_tokens: null },
    cwd: "",
    timestamp: "2026-01-01T00:00:00+00:00",
    status: "success",
    data,
  };
}

describe("RuntimeState message projection", () => {
  it("joins only assistant text blocks from a cumulative message", () => {
    const assistant = node({
      type: "message",
      message: {
        role: "assistant",
        content: [
          { type: "reasoning", text: "隐藏" },
          { type: "text", text: "第一" },
          { type: "tool_call", name: "read_file", call_id: "c1", arguments: {} },
          { type: "text", text: "第二" },
        ],
      },
    });

    expect(assistantContentFromRuntimeNode(assistant)).toBe("第一第二");
    expect(isAssistantMessageNode(assistant)).toBe(true);
  });

  it("ignores configuration and user nodes", () => {
    expect(assistantContentFromRuntimeNode(node({ type: "compaction", summary: "demo" }))).toBeNull();
    expect(assistantContentFromRuntimeNode(node({
      type: "message",
      message: { role: "user", content: [{ type: "text", text: "问题" }] },
    }))).toBeNull();
  });
});
