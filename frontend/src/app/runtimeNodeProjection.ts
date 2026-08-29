import type { RuntimeStateNode } from "../types";

type RuntimeMessageBlock = {
  type?: unknown;
  text?: unknown;
};

function messageFromNode(node: RuntimeStateNode): Record<string, unknown> | null {
  const message = node.data[node.current_data_idx]?.[1];
  return message ? message as Record<string, unknown> : null;
}

/** Return the cumulative assistant text carried by a RuntimeState message node. */
export function assistantContentFromRuntimeNode(node: RuntimeStateNode): string | null {
  const message = messageFromNode(node);
  if (!message || message.role !== "assistant") return null;
  if (!Array.isArray(message.content)) return "";

  return (message.content as RuntimeMessageBlock[])
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text as string)
    .join("");
}

/** Distinguish assistant message nodes from configuration/tool/tree nodes. */
export function isAssistantMessageNode(node: RuntimeStateNode): boolean {
  return assistantContentFromRuntimeNode(node) !== null;
}
