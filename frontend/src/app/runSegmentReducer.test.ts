import { describe, expect, it } from "vitest";
import type { ChatMessage, RunPresentationSegment } from "../types";
import { applyRunSegment } from "./runSegmentReducer";

const base: ChatMessage = { id: "a", role: "assistant", content: "", events: [] };

function segment(partial: Partial<RunPresentationSegment> & Pick<RunPresentationSegment, "sequence" | "segment_id" | "segment_type">): RunPresentationSegment {
  return { status: "streaming", ...partial };
}

describe("run segment reducer", () => {
  it("updates by segment_id and keeps same-type rounds separate", () => {
    let message = applyRunSegment(base, segment({ sequence: 2, segment_id: "r:2", segment_type: "thinking", text: "one" }));
    message = applyRunSegment(message, segment({ sequence: 1, segment_id: "r:1", segment_type: "thinking", text: "first" }));
    message = applyRunSegment(message, segment({ sequence: 2, segment_id: "r:2", segment_type: "thinking", text: "one updated", status: "completed" }));
    expect(message.segments?.map((item) => item.segment_id)).toEqual(["r:1", "r:2"]);
    expect(message.segments?.[1].text).toBe("one updated");
  });

  it("merges parallel tools by call_id", () => {
    let message = applyRunSegment(base, segment({
      sequence: 1,
      segment_id: "r:1",
      segment_type: "tool_batch",
      tools: [{ call_id: "a", name: "glob", arguments: {}, status: "pending" }],
    }));
    message = applyRunSegment(message, segment({
      sequence: 1,
      segment_id: "r:1",
      segment_type: "tool_batch",
      status: "completed",
      tools: [{ call_id: "a", name: "glob", arguments: {}, status: "succeeded", result: "ok" }, { call_id: "b", name: "run", arguments: {}, status: "succeeded" }],
    }));
    expect(message.segments?.[0].tools?.map((tool) => tool.call_id)).toEqual(["a", "b"]);
    expect(message.segments?.[0].tools?.[0].result).toBe("ok");
  });
});
