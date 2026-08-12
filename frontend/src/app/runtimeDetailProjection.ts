import type { ChatMessage, Conversation, RuntimeNodeFrame, RuntimeStateNode, ToolEvent } from "../types";
import { applyRuntimeNodeFrame } from "./runtimeNodeReducer";

function nodeKey(node: RuntimeStateNode): string {
  return `${node.session_id}:${node.id}`;
}

function messageData(node: RuntimeStateNode): Record<string, unknown> | null {
  const message = node.data.message;
  return message && typeof message === "object" && !Array.isArray(message)
    ? message as Record<string, unknown>
    : null;
}

function contentBlocks(message: Record<string, unknown>): Array<Record<string, unknown>> {
  return Array.isArray(message.content)
    ? message.content.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function lastIndex<T>(values: readonly T[], predicate: (value: T) => boolean): number {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (predicate(values[index])) return index;
  }
  return -1;
}

export interface ProjectedNodeDetails {
  role: string;
  content: string;
  events: ToolEvent[];
  runId?: string;
  error?: string;
}

function terminalError(message: Record<string, unknown>): string | undefined {
  const raw = message.error;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const error = raw as Record<string, unknown>;
  const summary = typeof error.message === "string" ? error.message.trim() : "";
  const detail = typeof error.detail === "string" ? error.detail.trim() : "";
  if (!summary) return undefined;
  return detail ? `${summary}\n\nDetails: ${detail}` : summary;
}

function terminalNodeError(
  node: RuntimeStateNode,
  message?: Record<string, unknown> | null,
  terminal = true,
): string | undefined {
  if (!terminal) return undefined;
  if (node.status !== "failed" && node.status !== "abort") return undefined;
  const embedded = message ? terminalError(message) : undefined;
  if (embedded) return embedded;
  if (node.status === "failed") return "An unknown error caused the system to encounter an exception.";
  if (node.status === "abort") return "The run was aborted for an unknown reason.";
  return undefined;
}

export function projectRuntimeNode(node: RuntimeStateNode, terminal = true): ProjectedNodeDetails | null {
  const message = messageData(node);
  if (!message) {
    const error = terminalNodeError(node, null, terminal);
    return error ? { role: "assistant", content: error, events: [], error } : null;
  }
  const role = String(message.role ?? "");
  const key = nodeKey(node);
  const events: ToolEvent[] = [];
  const answers: string[] = [];
  const reasoning: string[] = [];

  for (const block of contentBlocks(message)) {
    const kind = String(block.type ?? "");
    if (kind === "reasoning") {
      reasoning.push(textValue(block.text));
    } else if (kind === "text" || kind === "bash") {
      answers.push(textValue(block.text));
    } else if (kind === "tool_call") {
      const tool = String(block.name ?? block.tool ?? "工具");
      events.push({
        kind: "tool_call",
        message: tool,
        data: {
          ...block,
          tool,
          call_id: String(block.call_id ?? ""),
          arguments: block.arguments ?? {},
          node_key: key,
        },
      });
    } else if (kind === "tool_result") {
      const failed = block.status === "failed";
      if (failed) continue;
      events.push({
        kind: failed ? "tool_failed" : "tool_result",
        message: textValue(block.content),
        data: {
          ...block,
          tool: String(block.tool ?? "工具"),
          call_id: String(block.call_id ?? ""),
          result: block.content,
          node_key: key,
        },
      });
    }
  }
  if (reasoning.some(Boolean)) {
    events.unshift({
      kind: "thinking",
      message: reasoning.join(""),
      data: { node_key: key, completed: terminal && node.status !== "abort" },
    });
  }
  return {
    role,
    content: answers.join(""),
    events,
    runId: typeof message.run_id === "string" ? message.run_id : undefined,
    error: terminalNodeError(node, message, terminal),
  };
}

function projectMessageNodes(
  message: ChatMessage,
  nodes: Map<string, RuntimeStateNode>,
  activeNodeKey?: string,
): ChatMessage {
  const projections = (message.runtimeNodeIds ?? [])
    .map((key) => ({ key, node: nodes.get(key) }))
    .filter((item): item is { key: string; node: RuntimeStateNode } => Boolean(item.node))
    .map(({ key, node }) => projectRuntimeNode(node, key !== activeNodeKey))
    .filter((item): item is ProjectedNodeDetails => Boolean(item));
  if (projections.length === 0) return message;
  const projectedEvents = projections.flatMap((item) => item.events);
  const hasAssistantProjection = projections.some((item) => item.role === "assistant");
  const projectedError = [...projections].reverse().find((item) => item.error)?.error;
  const withoutPreviousError = { ...message };
  delete withoutPreviousError.error;
  return {
    ...withoutPreviousError,
    content: hasAssistantProjection ? projections.map((item) => item.content).join("") : message.content,
    events: hasAssistantProjection ? projectedEvents : message.events,
    runId: [...projections].reverse().find((item) => item.runId)?.runId ?? message.runId,
    ...(projectedError ? { error: projectedError } : {}),
  };
}

export function integrateRuntimeNodeFrame(conversation: Conversation, frame: RuntimeNodeFrame): Conversation {
  const current = new Map<string, RuntimeStateNode>(
    (conversation.runtimeNodes ?? []).map((node) => [nodeKey(node), node] as const),
  );
  const next = applyRuntimeNodeFrame(current, frame);
  const projection = projectRuntimeNode(frame.node, frame.type === "node.delete");
  const messages = [...conversation.messages];
  const key = nodeKey(frame.node);

  if (projection?.role === "user") {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role !== "user") continue;
      messages[index] = { ...messages[index], sourceNodeId: frame.node.parent_id || undefined };
      break;
    }
  } else if (projection?.role === "assistant" || projection?.role === "tool_result") {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role !== "assistant") continue;
      const runtimeNodeIds = messages[index].runtimeNodeIds?.includes(key)
        ? messages[index].runtimeNodeIds!
        : [...(messages[index].runtimeNodeIds ?? []), key];
      messages[index] = projectMessageNodes(
        { ...messages[index], runtimeNodeIds, sourceNodeId: frame.node.id },
        next,
        frame.type === "node.delete" ? undefined : key,
      );
      break;
    }
  }

  return {
    ...conversation,
    messages,
    runtimeNodes: [...next.values()],
    lastNodeId: frame.node.id,
  };
}

export function appendLegacyRuntimeEvent(message: ChatMessage, event: ToolEvent): ChatMessage {
  if (event.kind === "thinking_start") {
    return {
      ...message,
      events: [...message.events, { kind: "thinking", message: "", data: { streaming: true } }],
    };
  }
  if (event.kind === "thinking_delta") {
    const events = [...message.events];
    const index = lastIndex(events, (item) => item.kind === "thinking" && item.data?.streaming === true);
    if (index >= 0) events[index] = { ...events[index], message: events[index].message + event.message };
    else events.push({ kind: "thinking", message: event.message, data: { streaming: true } });
    return { ...message, events };
  }
  if (event.kind === "thinking_end") {
    const events = [...message.events];
    const index = lastIndex(events, (item) => item.kind === "thinking" && item.data?.streaming === true);
    if (index >= 0) events[index] = { ...events[index], data: { ...events[index].data, streaming: false } };
    return { ...message, events };
  }
  if (["tool_call", "tool_result"].includes(event.kind)) {
    return { ...message, events: [...message.events, event] };
  }
  return message;
}
