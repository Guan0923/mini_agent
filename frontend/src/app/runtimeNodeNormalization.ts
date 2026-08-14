import type {
  ChatMode,
  PermissionMode,
  ReasoningEffort,
  RuntimeNodeModel,
  RuntimeNodeUsage,
  RuntimeStateNode,
  ThinkingMode,
} from "../types";

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
const PERMISSION_MODES = new Set<PermissionMode>(["approval_for_me", "full_access"]);
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
 * Repair browser/API nodes at the frontend trust boundary.
 *
 * The backend owns strict protocol validation. The browser can still contain
 * a cached pre-v0.3 node, or receive a partial frame while upgrading. A bad
 * historical node must not be able to unmount the entire React application.
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

export function normalizeRuntimeNode(node: RuntimeStateNode): RuntimeStateNode {
  const usage = objectValue(node.usage);
  return {
    ...node,
    provider_name: typeof node.provider_name === "string" ? node.provider_name : "unknown",
    model: normalizeRuntimeNodeModel(node.model),
    permission_mode: PERMISSION_MODES.has(node.permission_mode) ? node.permission_mode : "approval_for_me",
    running_mode: RUNNING_MODES.has(node.running_mode) ? node.running_mode : "agent",
    usage: {
      input_tokens: tokenCount(usage.input_tokens),
      cached_tokens: tokenCount(usage.cached_tokens),
      output_tokens: tokenCount(usage.output_tokens),
      reasoning_tokens: tokenCount(usage.reasoning_tokens),
      total_tokens: tokenCount(usage.total_tokens),
    },
  };
}
