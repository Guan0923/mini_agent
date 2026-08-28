import type {
  ChatMode,
  PermissionMode,
  ReasoningEffort,
  RuntimeNodeModel,
  RuntimeNodeUsage,
  RuntimeRootNode,
  RuntimeStateNode,
  RuntimeTreeNode,
  ThinkingMode,
} from "../../types";

export const DEFAULT_RUNTIME_NODE_MODEL: RuntimeNodeModel = {
  reasoning_effort: "medium",
  current_model: "unknown",
  context_length: 128000,
  output_length: 8192,
  thinking: "enable",
  temperature: 1,
};

export const EMPTY_RUNTIME_NODE_USAGE: RuntimeNodeUsage = {
  input_tokens: null,
  cached_tokens: null,
  output_tokens: null,
  reasoning_tokens: null,
  total_tokens: null,
};

const REASONING_EFFORTS = new Set<ReasoningEffort>(["low", "medium", "high", "xhigh", "max"]);
const THINKING_MODES = new Set<ThinkingMode>(["enable", "disable"]);
const PERMISSION_MODES = new Set<PermissionMode>(["read_only", "workspace_write", "full_access"]);
const RUNNING_MODES = new Set<ChatMode>(["agent", "plan"]);

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function positiveInteger(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : fallback;
}

function tokenCount(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/**
 * Validate the new protocol at the frontend trust boundary. Old cached node
 * shapes are deliberately rejected instead of normalized.
 */
export function normalizeRuntimeNodeModel(value: unknown): RuntimeNodeModel {
  const raw = objectValue(value);
  const outputLength = positiveInteger(raw.output_length, DEFAULT_RUNTIME_NODE_MODEL.output_length);
  let contextLength = positiveInteger(raw.context_length, DEFAULT_RUNTIME_NODE_MODEL.context_length);
  if (contextLength <= outputLength) contextLength = Math.max(DEFAULT_RUNTIME_NODE_MODEL.context_length, outputLength + 1);
  return {
    reasoning_effort: REASONING_EFFORTS.has(raw.reasoning_effort as ReasoningEffort)
      ? raw.reasoning_effort as ReasoningEffort
      : DEFAULT_RUNTIME_NODE_MODEL.reasoning_effort,
    current_model: typeof raw.current_model === "string" && raw.current_model.trim()
      ? raw.current_model
      : DEFAULT_RUNTIME_NODE_MODEL.current_model,
    context_length: contextLength,
    output_length: outputLength,
    thinking: THINKING_MODES.has(raw.thinking as ThinkingMode)
      ? raw.thinking as ThinkingMode
      : DEFAULT_RUNTIME_NODE_MODEL.thinking,
    temperature: typeof raw.temperature === "number" && raw.temperature >= 0 && raw.temperature <= 2
      ? raw.temperature
      : DEFAULT_RUNTIME_NODE_MODEL.temperature,
  };
}

export function isRuntimeRootNode(node: unknown): node is RuntimeRootNode {
  if (!node || typeof node !== "object" || Array.isArray(node)) return false;
  const raw = node as Record<string, unknown>;
  const keys = Object.keys(raw).sort();
  return keys.length === 3
    && keys[0] === "id"
    && keys[1] === "session_id"
    && keys[2] === "thread_id"
    && typeof raw.id === "string" && raw.id.length > 0
    && typeof raw.session_id === "string" && raw.session_id.length > 0
    && raw.thread_id === raw.session_id;
}

export function isRuntimeTurnNode(node: RuntimeTreeNode): node is RuntimeStateNode {
  return "version" in node;
}

export function normalizeRuntimeNode(node: RuntimeStateNode): RuntimeStateNode;
export function normalizeRuntimeNode(node: RuntimeRootNode): RuntimeRootNode;
export function normalizeRuntimeNode(node: RuntimeTreeNode): RuntimeTreeNode;
export function normalizeRuntimeNode(node: RuntimeTreeNode): RuntimeTreeNode {
  if (isRuntimeRootNode(node)) return structuredClone(node);
  const turn = node as RuntimeStateNode;
  if (!turn || typeof turn !== "object" || turn.version !== "0.0.1") {
    throw new Error("Unsupported Turn version");
  }
  for (const key of ["thread_id", "parent_thread_id", "session_id", "parent_session_id", "id", "parent_id", "user", "provider_name", "cwd", "timestamp"] as const) {
    if (typeof turn[key] !== "string") throw new Error(`Invalid Turn field: ${key}`);
  }
  if (!turn.thread_id || !turn.session_id || !turn.id || !turn.provider_name) throw new Error("Turn identifiers are required");
  if (!Number.isInteger(turn.firstKeptItemSize) || turn.firstKeptItemSize < 0) throw new Error("Invalid firstKeptItemSize");
  if (typeof turn.compactionId !== "string" || !turn.compactionId) throw new Error("Invalid compactionId");
  if (!["running", "success", "paused", "failed"].includes(turn.status)) throw new Error("Invalid Turn status");
  if (!PERMISSION_MODES.has(turn.permission_mode) || !RUNNING_MODES.has(turn.running_mode)) throw new Error("Invalid Turn mode");
  if (!Array.isArray(turn.data) || !Number.isInteger(turn.current_data_idx) || !turn.data[turn.current_data_idx]) {
    throw new Error("Invalid Turn data/current_data_idx");
  }
  for (const version of turn.data) {
    if (!Array.isArray(version) || version.length === 0) throw new Error("A Turn version must contain Messages");
    for (let index = 0; index < version.length; index += 1) {
      const message = version[index];
      const expectedRole = index % 2 === 0 ? "user" : "assistant";
      if (message?.role !== expectedRole || !Array.isArray(message.content)) {
        throw new Error("Turn Messages must alternate user and assistant");
      }
      if (expectedRole === "user" && (message.content.length !== 1 || message.content[0]?.type !== "text" || typeof message.content[0]?.text !== "string")) {
        throw new Error("A user Message must contain one text Item");
      }
    }
    if (turn.status !== "running" && version[version.length - 1]?.role !== "assistant") throw new Error("A non-running Turn must end with assistant");
  }
  const model = objectValue(turn.model);
  const normalizedModel = normalizeRuntimeNodeModel(model);
  if (Object.keys(normalizedModel).some((key) => model[key] !== normalizedModel[key as keyof RuntimeNodeModel])) {
    throw new Error("Invalid Turn model");
  }
  const usage = objectValue(turn.usage);
  for (const key of ["input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "total_tokens"]) {
    if (!(key in usage) || (usage[key] !== null && tokenCount(usage[key]) === null)) throw new Error("Invalid Turn usage");
  }
  return structuredClone(turn);
}
