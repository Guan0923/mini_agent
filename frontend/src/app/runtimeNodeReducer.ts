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
  const key = `${frame.node.session_id}:${frame.node.id}`;
  if (frame.type === "node.create" || frame.type === "node.update" || frame.type === "node.delete") {
    next.set(key, normalizeRuntimeNode(JSON.parse(JSON.stringify(frame.node)) as RuntimeStateNode));
  }
  return next;
}

export function nodeFrame(message: { type: NodeFrameType; node?: RuntimeStateNode }): RuntimeNodeFrame | null {
  return message.node ? { type: message.type, node: message.node } : null;
}

export function leafNodes(nodes: Iterable<RuntimeStateNode>, sessionId?: string): RuntimeStateNode[] {
  const all = [...nodes].filter(
    (node) => node.data.type !== "root" && (!sessionId || node.session_id === sessionId),
  );
  const parentKeys = new Set(all.map((node) => `${node.parent_session_id}:${node.parent_id}`));
  return all.filter((node) => !parentKeys.has(`${node.session_id}:${node.id}`));
}
