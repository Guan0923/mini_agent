import type { NodeFrameType, RuntimeNodeFrame, RuntimeStateNode } from "../types";
import { normalizeRuntimeNode } from "./runtimeNodeNormalization";

/**
 * Apply complete node lifecycle frames. Updates replace the dynamic view in
 * full; delete keeps the final static node in the tree for history/recovery.
 */
export function applyRuntimeNodeFrame(
  nodes: Map<string, RuntimeStateNode>,
  frame: RuntimeNodeFrame,
): Map<string, RuntimeStateNode> {
  const next = new Map(nodes);
  const key = `${frame.turn.session_id}:${frame.turn.id}`;
  next.set(key, normalizeRuntimeNode(structuredClone(frame.turn)));
  return next;
}

export function nodeFrame(message: { type: NodeFrameType; turn?: RuntimeStateNode }): RuntimeNodeFrame | null {
  return message.turn ? { type: message.type, turn: message.turn } : null;
}

export function leafNodes(nodes: Iterable<RuntimeStateNode>, sessionId?: string): RuntimeStateNode[] {
  const all = [...nodes].filter(
    (node) => !sessionId || node.session_id === sessionId,
  );
  const parentKeys = new Set(all.map((node) => `${node.parent_session_id}:${node.parent_id}`));
  return all.filter((node) => !parentKeys.has(`${node.session_id}:${node.id}`));
}
