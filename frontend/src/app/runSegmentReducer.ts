import type { ChatMessage, RunPresentationSegment, RunPresentationTool } from "../types";

function mergeTools(previous: RunPresentationTool[] = [], incoming: RunPresentationTool[] = []): RunPresentationTool[] {
  const byId = new Map(previous.map((tool) => [tool.call_id, tool]));
  for (const tool of incoming) {
    if (!tool.call_id) continue;
    byId.set(tool.call_id, { ...byId.get(tool.call_id), ...tool });
  }
  return [...byId.values()];
}

export function mergeRunSegment(
  previous: RunPresentationSegment | undefined,
  incoming: RunPresentationSegment,
): RunPresentationSegment {
  const merged: RunPresentationSegment = { ...previous, ...incoming };
  if (previous?.tools || incoming.tools) merged.tools = mergeTools(previous?.tools, incoming.tools);
  return merged;
}

/** Apply one ordered presentation snapshot without deriving order from nodes/events. */
export function applyRunSegment(message: ChatMessage, incoming: RunPresentationSegment): ChatMessage {
  if (!incoming.segment_id || !Number.isFinite(incoming.sequence)) return message;
  const byId = new Map((message.segments ?? []).map((segment) => [segment.segment_id, segment]));
  byId.set(incoming.segment_id, mergeRunSegment(byId.get(incoming.segment_id), incoming));
  const segments = [...byId.values()].sort((left, right) => left.sequence - right.sequence);
  const response = [...segments].reverse().find((segment) => segment.segment_type === "response" && segment.text);
  return {
    ...message,
    segments,
    // Keep the canonical answer field for copy/history/fallback, while the
    // segments remain the only source of the live visual ordering.
    content: response?.text ?? message.content,
  };
}
