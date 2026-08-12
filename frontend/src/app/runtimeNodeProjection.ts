import type { RuntimeStateNode } from "../types";

type RuntimeMessageBlock = {
  type?: unknown;
  text?: unknown;
};

function messageFromNode(node: RuntimeStateNode): Record<string, unknown> | null {
  if (node.data.type !== "message") return null;
  const message = node.data.message;
  if (!message || typeof message !== "object" || Array.isArray(message)) return null;
  return message as Record<string, unknown>;
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
