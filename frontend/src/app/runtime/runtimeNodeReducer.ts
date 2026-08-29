import type { NodeFrameType, RuntimeNodeFrame, RuntimeStateNode, RuntimeTreeNode, TurnItem } from "../../types";
import { isRuntimeTurnNode, normalizeRuntimeNode, TURN_PROTOCOL_VERSION } from "./runtimeNodeNormalization";

export interface RuntimeNodeAccumulator {
  nodes: Map<string, RuntimeStateNode>;
  revisions: Map<string, number>;
}

const PATCH_FIELDS = new Set<keyof RuntimeStateNode>([
  "version",
  "firstKeptItemSize",
  "compactionId",
  "user",
  "provider_name",
  "model",
  "permission_mode",
  "running_mode",
  "usage",
  "cwd",
  "project_cwd",
  "timestamp",
  "status",
  "current_data_idx",
]);

const STATUSES = new Set(["running", "success", "paused", "failed"]);
const PERMISSION_MODES = new Set(["read_only", "workspace_write", "full_access"]);
const RUNNING_MODES = new Set(["agent", "plan"]);
const REASONING_EFFORTS = new Set(["low", "medium", "high", "xhigh", "max"]);
const THINKING_MODES = new Set(["enable", "disable"]);
const USAGE_FIELDS = ["input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isTokenCount(value: unknown): boolean {
  return value === null || (typeof value === "number" && Number.isInteger(value) && value >= 0);
}

function validatePatchValue(name: keyof RuntimeStateNode, value: unknown): void {
  if (name === "version" && value === TURN_PROTOCOL_VERSION) return;
  if (name === "firstKeptItemSize" && Number.isInteger(value) && (value as number) >= 0) return;
  if (name === "current_data_idx" && Number.isInteger(value) && (value as number) >= 0) return;
  if (name === "compactionId" && typeof value === "string" && value.length > 0) return;
  if (["user", "cwd", "project_cwd", "timestamp"].includes(name) && typeof value === "string") return;
  if (name === "provider_name" && typeof value === "string" && value.length > 0) return;
  if (name === "status" && STATUSES.has(String(value))) return;
  if (name === "permission_mode" && PERMISSION_MODES.has(String(value))) return;
  if (name === "running_mode" && RUNNING_MODES.has(String(value))) return;
  if (name === "usage" && isRecord(value)
    && Object.keys(value).length === USAGE_FIELDS.length
    && USAGE_FIELDS.every((field) => field in value && isTokenCount(value[field]))) return;
  if (name === "model" && isRecord(value)
    && REASONING_EFFORTS.has(String(value.reasoning_effort))
    && typeof value.current_model === "string" && value.current_model.length > 0
    && typeof value.context_length === "number" && Number.isInteger(value.context_length) && value.context_length > 0
    && typeof value.output_length === "number" && Number.isInteger(value.output_length) && value.output_length > 0
    && value.context_length > value.output_length
    && THINKING_MODES.has(String(value.thinking))
    && typeof value.temperature === "number" && value.temperature >= 0 && value.temperature <= 2) return;
  throw new Error(`Turn delta patch value is invalid for ${name}`);
}

export function runtimeNodeAccumulator(): RuntimeNodeAccumulator {
  return { nodes: new Map(), revisions: new Map() };
}

function frameKey(frame: RuntimeNodeFrame): string {
  return frame.type === "turn.snapshot"
    ? `${frame.turn.session_id}:${frame.turn.id}`
    : `${frame.session_id}:${frame.turn_id}`;
}

/**
 * Apply one baseline snapshot followed by strictly ordered, append-only Turn deltas.
 */
export function applyRuntimeNodeFrame(
  accumulator: RuntimeNodeAccumulator,
  frame: RuntimeNodeFrame,
): RuntimeStateNode {
  const key = frameKey(frame);
  if (frame.type === "turn.snapshot") {
    if (frame.revision !== 0) throw new Error("Turn snapshot revision must be zero");
    if (accumulator.revisions.has(key)) throw new Error("Turn received more than one baseline snapshot");
    const turn = normalizeRuntimeNode(frame.turn);
    accumulator.nodes.set(key, turn);
    accumulator.revisions.set(key, 0);
    return turn;
  }

  const previous = accumulator.nodes.get(key);
  if (!previous) throw new Error("Turn delta arrived before its baseline snapshot");
  const previousRevision = accumulator.revisions.get(key);
  if (previousRevision === undefined || !Number.isInteger(frame.revision) || frame.revision !== previousRevision + 1) {
    throw new Error("Turn delta revision is not consecutive");
  }
  let turn: RuntimeStateNode = { ...previous };
  if (frame.patch) {
    if (!isRecord(frame.patch)) throw new Error("Turn delta patch is invalid");
    for (const [name, value] of Object.entries(frame.patch)) {
      if (!PATCH_FIELDS.has(name as keyof RuntimeStateNode)) throw new Error(`Turn delta cannot patch ${name}`);
      validatePatchValue(name as keyof RuntimeStateNode, value);
    }
    turn = { ...turn, ...structuredClone(frame.patch) };
  }

  if (frame.operations !== undefined && !Array.isArray(frame.operations)) {
    throw new Error("Turn delta operations are invalid");
  }
  if ((!frame.patch || Object.keys(frame.patch).length === 0) && (frame.operations?.length ?? 0) === 0) {
    throw new Error("Turn delta must contain a patch or operation");
  }

  let data = previous.data;
  const mutableItems = new Map<string, TurnItem[]>();
  const itemsAt = (dataIdx: number, messageIdx: number): TurnItem[] => {
    const cacheKey = `${dataIdx}:${messageIdx}`;
    const cached = mutableItems.get(cacheKey);
    if (cached) return cached;
    const version = data[dataIdx];
    if (!version || version[messageIdx]?.role !== "assistant" || !Array.isArray(version[messageIdx].content)) {
      throw new Error("Turn delta targets invalid assistant content");
    }
    if (data === previous.data) data = [...previous.data];
    const messages = data[dataIdx] === version ? [...version] : data[dataIdx];
    const items = [...version[messageIdx].content];
    messages[messageIdx] = { ...version[messageIdx], content: items };
    data[dataIdx] = messages;
    mutableItems.set(cacheKey, items);
    return items;
  };

  for (const operation of frame.operations ?? []) {
    if (!Number.isInteger(operation.data_idx) || operation.data_idx < 0) {
      throw new Error("Turn delta data index is invalid");
    }
    if (!Number.isInteger(operation.message_idx) || operation.message_idx < 0) {
      throw new Error("Turn delta message index is invalid");
    }
    if (operation.op === "append_message") {
      const version = data[operation.data_idx];
      if (!version || operation.message_idx !== version.length || !isRecord(operation.message)) {
        throw new Error("Turn Message delta is out of order");
      }
      const expectedRole = operation.message_idx % 2 === 0 ? "user" : "assistant";
      if (operation.message.role !== expectedRole || !Array.isArray(operation.message.content)) {
        throw new Error("Turn Message delta breaks role alternation");
      }
      if (expectedRole === "user" && (
        operation.message.content.length !== 1
        || !isRecord(operation.message.content[0])
        || operation.message.content[0].type !== "text"
        || typeof operation.message.content[0].text !== "string"
      )) {
        throw new Error("Turn user Message delta is invalid");
      }
      if (data === previous.data) data = [...previous.data];
      data[operation.data_idx] = [...version, structuredClone(operation.message)];
      continue;
    }
    if (!Number.isInteger(operation.item_idx) || operation.item_idx < 0) {
      throw new Error("Turn delta item index is invalid");
    }
    const items = itemsAt(operation.data_idx, operation.message_idx);
    if (operation.op === "append_item") {
      if (operation.item_idx !== items.length) throw new Error("Turn item delta is out of order");
      if (
        !isRecord(operation.item)
        || typeof operation.item.type !== "string"
        || !operation.item.type
        || !["running", "failed", "success"].includes(String(operation.item.status))
      ) {
        throw new Error("Turn item delta is invalid");
      }
      items.push(structuredClone(operation.item));
      continue;
    }
    if (operation.op === "set_item_status") {
      if (!["running", "failed", "success"].includes(operation.status)) {
        throw new Error("Turn Item status delta is invalid");
      }
      const item = items[operation.item_idx];
      if (!item) throw new Error("Turn Item status delta target is missing");
      items[operation.item_idx] = { ...item, status: operation.status };
      continue;
    }
    if (operation.op !== "append_text" || typeof operation.delta !== "string" || !operation.delta) {
      throw new Error("Turn delta operation is invalid");
    }
    const item = items[operation.item_idx];
    if (!item || !["text", "reasoning"].includes(item.type) || typeof item.text !== "string") {
      throw new Error("Turn text delta targets a non-streaming Item");
    }
    items[operation.item_idx] = { ...item, text: item.text + operation.delta };
  }
  turn.data = data;
  if (!turn.data[turn.current_data_idx]) throw new Error("Turn delta produced an invalid data index");
  if (turn.status !== "running" && turn.data.some((version) => version[version.length - 1]?.role !== "assistant")) {
    throw new Error("A non-running Turn must end with assistant");
  }
  accumulator.nodes.set(key, turn);
  accumulator.revisions.set(key, frame.revision);
  return turn;
}

export function nodeFrame(message: RuntimeNodeFrame & { type: NodeFrameType }): RuntimeNodeFrame {
  return message;
}

export function leafNodes(nodes: Iterable<RuntimeTreeNode>, sessionId?: string): RuntimeStateNode[] {
  const all = [...nodes].filter(isRuntimeTurnNode).filter(
    (node) => !sessionId || node.session_id === sessionId,
  );
  const parentKeys = new Set(all.map((node) => `${node.parent_session_id}:${node.parent_id}`));
  return all.filter((node) => !parentKeys.has(`${node.session_id}:${node.id}`));
}
