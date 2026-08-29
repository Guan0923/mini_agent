import type { ChatMessage, Conversation, DecisionRequest, FileReference, RuntimeStateNode, ToolEvent, TurnItem } from "../types";
import { normalizeRuntimeNode } from "./runtimeNodeNormalization";

const keyOf = (turn: RuntimeStateNode) => `${turn.session_id}:${turn.id}`;

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function references(item: TurnItem): FileReference[] | undefined {
  const raw = item.references;
  if (!Array.isArray(raw)) return undefined;
  const result = raw.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const record = value as Record<string, unknown>;
    return (record.source === "project" || record.source === "upload") && typeof record.path === "string"
      ? [{ source: record.source, path: record.path } as FileReference]
      : [];
  });
  return result.length ? result : undefined;
}

function assistantMessageIndex(turn: RuntimeStateNode): number {
  const selected = turn.data[turn.current_data_idx] ?? [];
  for (let index = selected.length - 1; index >= 0; index -= 1) {
    if (selected[index]?.role === "assistant") return index;
  }
  return -1;
}

function visibleAssistantItems(turn: RuntimeStateNode, messageIdx = assistantMessageIndex(turn)): TurnItem[] {
  const selected = turn.data[turn.current_data_idx];
  const items = selected?.[messageIdx]?.role === "assistant" ? selected[messageIdx].content : [];
  if (items[0]?.type !== "compaction") return items;
  const kept = Number(items[0].kept_item_count ?? 0);
  return items.slice(1 + Math.max(0, kept));
}

function isToolApproval(item: TurnItem): boolean {
  return item.type === "approval" && typeof item.tool === "string" && item.tool.length > 0;
}

function displayAssistantItems(turn: RuntimeStateNode, items: TurnItem[]): TurnItem[] {
  const toolResults = new Map<string, TurnItem>();
  for (const item of items) {
    if (item.type === "tool_result" && typeof item.call_id === "string") toolResults.set(item.call_id, item);
  }

  const displayed: TurnItem[] = [];
  for (let index = 0; index < items.length;) {
    const item = items[index];
    if (item.type === "skill_snapshot") {
      index += 1;
      continue;
    }
    if (!isToolApproval(item)) {
      displayed.push({ ...item });
      index += 1;
      continue;
    }

    const tool = String(item.tool);
    const approvals: TurnItem[] = [];
    while (index < items.length && isToolApproval(items[index]) && String(items[index].tool) === tool) {
      approvals.push(items[index]);
      index += 1;
    }
    const decision = [...approvals].reverse().find(
      (approval) => approval.event === "decision_requested" && typeof approval.decision_id === "string",
    );
    const callId = approvals.find((approval) => typeof approval.call_id === "string")?.call_id;
    const result = typeof callId === "string" ? toolResults.get(callId) : undefined;
    const granted = approvals.some((approval) => approval.event === "approval_granted");

    if (result || granted) {
      displayed.push({
        ...(decision ?? approvals[0]),
        type: "approval",
        event: "approval_resolved",
        approval_status: result?.failure_code === "user_denied" ? "denied" : "allowed",
        call_id: callId,
        tool,
        text: "",
      });
    } else if (turn.status === "running" && decision) {
      displayed.push({ ...decision, call_id: callId, tool });
    }
  }
  return displayed;
}

function itemEvents(turn: RuntimeStateNode, items: TurnItem[], messageIdx: number): ToolEvent[] {
  const events: ToolEvent[] = [];
  if (turn.data[turn.current_data_idx]?.[messageIdx]?.content?.[0]?.type === "compaction") {
    events.push({ kind: "compaction", message: "上下文已压缩" });
  }
  for (const item of items) {
    if (item.type === "reasoning") {
      events.push({ kind: "thinking", message: text(item.text), data: { completed: turn.status !== "running" } });
    } else if (item.type === "tool_call") {
      events.push({ kind: "tool_call", message: String(item.name ?? "工具"), data: { ...item } });
    } else if (item.type === "tool_result") {
      events.push({ kind: item.status === "failed" ? "tool_failed" : "tool_result", message: text(item.content), data: { ...item } });
    } else if (["approval", "question", "plan", "subagent", "skill_snapshot"].includes(item.type)) {
      events.push({ kind: item.type, message: text(item.text), data: { ...item } });
    }
  }
  return events;
}

function pendingDecision(items: TurnItem[], status: RuntimeStateNode["status"]): DecisionRequest | undefined {
  if (status !== "running") return undefined;
  const decisionItems = items.filter((item) => item.type === "approval" || item.type === "question");
  const latest = decisionItems[decisionItems.length - 1];
  if (!latest || latest.event !== "decision_requested" || typeof latest.decision_id !== "string") return undefined;
  const kind = latest.kind;
  if (!["tool", "plan", "question", "resume", "skill"].includes(String(kind))) return undefined;
  return {
    decision_id: latest.decision_id,
    kind: kind as DecisionRequest["kind"],
    message: typeof latest.text === "string" ? latest.text : undefined,
    tool: typeof latest.tool === "string" ? latest.tool : undefined,
    arguments: latest.arguments as DecisionRequest["arguments"],
    plan: typeof latest.plan === "string" ? latest.plan : undefined,
    goal: typeof latest.goal === "string" ? latest.goal : undefined,
    steps: Array.isArray(latest.steps) ? latest.steps.map(String) : undefined,
    details: typeof latest.details === "string" ? latest.details : undefined,
    questions: Array.isArray(latest.questions) ? latest.questions as DecisionRequest["questions"] : undefined,
    skill: typeof latest.skill === "string" ? latest.skill : undefined,
    description: typeof latest.description === "string" ? latest.description : undefined,
    project_id: typeof latest.project_id === "string" ? latest.project_id : undefined,
    workspace_sha256: typeof latest.workspace_sha256 === "string" ? latest.workspace_sha256 : undefined,
    tree_sha256: typeof latest.tree_sha256 === "string" ? latest.tree_sha256 : undefined,
    path: typeof latest.path === "string" ? latest.path : undefined,
  };
}

export interface ProjectedNodeDetails {
  role: string;
  content: string;
  events: ToolEvent[];
  items: TurnItem[];
  compactionNotice: boolean;
  error?: string;
  references?: FileReference[];
  status?: RuntimeStateNode["status"];
  decision?: DecisionRequest;
}

export function projectRuntimeNode(turn: RuntimeStateNode, messageIdx = assistantMessageIndex(turn)): ProjectedNodeDetails {
  const sourceItems = visibleAssistantItems(turn, messageIdx);
  const items = displayAssistantItems(turn, sourceItems);
  const errorItem = [...items].reverse().find((item) => item.type === "error");
  return {
    role: "assistant",
    content: items.filter((item) => item.type === "text" || item.type === "bash").map((item) => text(item.text)).join(""),
    events: itemEvents(turn, sourceItems, messageIdx),
    items,
    compactionNotice: turn.data[turn.current_data_idx]?.[messageIdx]?.content?.[0]?.type === "compaction",
    error: errorItem ? text(errorItem.message) : undefined,
    status: turn.status,
    decision: pendingDecision(items, turn.status),
  };
}

function ancestry(nodes: Map<string, RuntimeStateNode>, active: RuntimeStateNode): RuntimeStateNode[] {
  const path: RuntimeStateNode[] = [];
  const seen = new Set<string>();
  let current: RuntimeStateNode | undefined = active;
  while (current) {
    const key = keyOf(current);
    if (seen.has(key)) throw new Error("Turn ancestry contains a cycle");
    seen.add(key);
    path.push(current);
    if (!current.parent_id) break;
    current = nodes.get(`${current.parent_session_id}:${current.parent_id}`);
    if (!current) throw new Error("Turn ancestry is incomplete");
  }
  return path.reverse();
}

export function projectTurnPath(nodes: Map<string, RuntimeStateNode>, activeTurnId: string): ChatMessage[] {
  const active = [...nodes.values()].find((turn) => turn.id === activeTurnId);
  if (!active) return [];
  return ancestry(nodes, active).flatMap((turn) => {
    const selected = turn.data[turn.current_data_idx];
    if (!selected) return [];
    const result: ChatMessage[] = [];
    for (let messageIdx = 0; messageIdx < selected.length; messageIdx += 1) {
      const message = selected[messageIdx];
      if (message.role === "user") {
        const next = selected[messageIdx + 1];
        const compact = next?.role === "assistant" && next.content[0]?.type === "compaction";
        if (compact) continue;
        const userItem = message.content[0];
        result.push({
          id: `${turn.id}:message:${messageIdx}`,
          role: "user",
          content: text(userItem?.text),
          events: [],
          nodeId: turn.id,
          sourceNodeId: turn.parent_id || undefined,
          references: userItem ? references(userItem) : undefined,
          timelineSource: typeof message.steering_id === "string" ? "steering" : "user",
        });
        continue;
      }
      const assistant = projectRuntimeNode(turn, messageIdx);
      const isLatestMessage = messageIdx === selected.length - 1;
      if (assistant.compactionNotice || assistant.content || assistant.events.length || assistant.error || (turn.status === "running" && isLatestMessage)) {
        result.push({
          id: `${turn.id}:message:${messageIdx}`,
          role: "assistant",
          content: assistant.content,
          events: assistant.events,
          items: assistant.items,
          itemVersion: turn.current_data_idx,
          compactionNotice: assistant.compactionNotice,
          status: isLatestMessage ? turn.status : undefined,
          error: assistant.error,
          running: isLatestMessage && turn.status === "running",
          decision: isLatestMessage ? assistant.decision : undefined,
          sourceNodeId: turn.id,
          runtimeNodeIds: [keyOf(turn)],
        });
      }
    }
    return result;
  });
}

export function messagesBeforeRewind(messages: ChatMessage[], turnId: string): ChatMessage[] {
  const rewindIndex = messages.findIndex((message) => message.nodeId === turnId);
  return rewindIndex >= 0 ? messages.slice(0, rewindIndex) : messages;
}

export function pruneTurnDescendants(nodes: RuntimeStateNode[], turnId: string): RuntimeStateNode[] {
  const target = nodes.find((node) => node.id === turnId);
  if (!target) return nodes;

  const childrenByParent = new Map<string, RuntimeStateNode[]>();
  for (const node of nodes) {
    if (node.session_id !== target.session_id || node.thread_id !== target.thread_id || !node.parent_id) continue;
    const parentKey = `${node.parent_session_id}:${node.parent_id}`;
    const children = childrenByParent.get(parentKey);
    if (children) children.push(node);
    else childrenByParent.set(parentKey, [node]);
  }

  const descendantKeys = new Set<string>();
  const pending = [...(childrenByParent.get(keyOf(target)) ?? [])];
  while (pending.length > 0) {
    const node = pending.pop()!;
    const key = keyOf(node);
    if (descendantKeys.has(key)) continue;
    descendantKeys.add(key);
    pending.push(...(childrenByParent.get(key) ?? []));
  }
  if (descendantKeys.size === 0) return nodes;
  return nodes.filter((node) => !descendantKeys.has(keyOf(node)));
}

export function integrateRuntimeNodeUpdates(
  conversation: Conversation,
  turns: RuntimeStateNode[],
  activeTurnId: string,
  forcePathProjection: boolean,
): Conversation {
  const current = new Map((conversation.runtimeNodes ?? []).map((node) => {
    const normalized = normalizeRuntimeNode(node);
    return [keyOf(normalized), normalized] as const;
  }));
  for (const turn of turns) current.set(keyOf(turn), turn);
  const activeTurn = [...current.values()].find((turn) => turn.id === activeTurnId);
  if (!activeTurn) throw new Error("Active Turn is missing after applying an SSE frame");

  let messages: ChatMessage[];
  let assistantIndex = -1;
  for (let index = conversation.messages.length - 1; index >= 0; index -= 1) {
    const message = conversation.messages[index];
    if (message.role === "assistant" && message.sourceNodeId === activeTurnId) {
      assistantIndex = index;
      break;
    }
  }
  if (forcePathProjection || assistantIndex < 0) {
    messages = projectTurnPath(current, activeTurnId);
  } else {
    const projection = projectRuntimeNode(activeTurn);
    messages = [...conversation.messages];
    messages[assistantIndex] = {
      ...messages[assistantIndex],
      content: projection.content,
      events: projection.events,
      items: projection.items,
      itemVersion: activeTurn.current_data_idx,
      compactionNotice: projection.compactionNotice,
      status: activeTurn.status,
      error: projection.error,
      running: activeTurn.status === "running",
      decision: projection.decision,
      runtimeNodeIds: [keyOf(activeTurn)],
    };
  }
  return {
    ...conversation,
    runtimeNodes: [...current.values()],
    messages,
    activeTurnId,
    lastNodeId: activeTurnId,
    threadId: activeTurn.thread_id,
  };
}
