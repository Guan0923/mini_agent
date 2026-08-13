import { describe, expect, it } from "vitest";
import { applyRuntimeNodeFrame, leafNodes } from "./runtimeNodeReducer";
import type { RuntimeStateNode } from "../types";

const node = (id: string, parentId = "", status: RuntimeStateNode["status"] = "failed"): RuntimeStateNode => ({
  session_id: "s",
  parent_session_id: parentId ? "s" : "",
  id,
  parent_id: parentId,
  version: "0.3.0",
  firstKeptEntryId: id,
  compactionIdx: id,
  user: "",
  provider_name: "",
  model: { reasoning_effort: "medium", current_model: "demo", context_length: 128000, output_length: 8192, thinking: "enable", temperature: 1 },
  permission_mode: "approval_for_me",
  running_mode: "agent",
  usage: { input_tokens: null, cached_tokens: null, output_tokens: null, reasoning_tokens: null, total_tokens: null },
  cwd: "",
  timestamp: "2026-01-01T00:00:00+00:00",
  status,
  data: {},
});

describe("runtime node reducer", () => {
  it("replaces complete updates and keeps the final delete node", () => {
    let state = new Map<string, RuntimeStateNode>();
    state = applyRuntimeNodeFrame(state, { type: "node.create", node: node("a") });
    state = applyRuntimeNodeFrame(state, { type: "node.update", node: node("a", "", "failed") });
    state = applyRuntimeNodeFrame(state, { type: "node.delete", node: node("a", "", "success") });
    expect(state.get("s:a")?.status).toBe("success");
  });

  it("derives leaves from parent references", () => {
    expect(leafNodes([node("a"), node("b", "a")], "s").map((item) => item.id)).toEqual(["b"]);
  });
});
