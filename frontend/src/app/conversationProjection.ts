import { projectTurnPath } from "./runtime/runtimeDetailProjection";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import type { Conversation, RuntimeTreeNode } from "../types";

export function withLoadedTurns(
  conversation: Conversation,
  nodes: RuntimeTreeNode[],
  preferredActiveTurnId?: string,
): Conversation {
  const threadId = conversation.threadId ?? conversation.sessionId;
  const threadNodes = nodes.filter(isRuntimeTurnNode).filter((node) => node.thread_id === threadId);
  const selected = threadNodes.find((node) => node.id === (preferredActiveTurnId ?? conversation.activeTurnId));
  const parentIds = new Set(threadNodes.map((node) => node.parent_id).filter(Boolean));
  const leaves = threadNodes
    .filter((node) => !parentIds.has(node.id))
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
  const fallback = leaves[leaves.length - 1];
  const activeTurnId = selected?.id ?? fallback?.id;
  const map = new Map(nodes.map((node) => [`${node.session_id}:${node.id}`, node] as const));
  return {
    ...conversation,
    runtimeNodes: nodes,
    activeTurnId,
    lastNodeId: activeTurnId,
    messages: activeTurnId ? projectTurnPath(map, activeTurnId) : [],
    messagesLoaded: true,
  };
}
