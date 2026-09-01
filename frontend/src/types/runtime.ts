import type { ChatMode, PermissionMode, ReasoningEffort, ThinkingMode } from "./app";

export interface ToolEvent {
  kind: string;
  message: string;
  data?: Record<string, unknown>;
}

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoItem {
  content: string;
  status: TodoStatus;
}

export type RuntimeNodeStatus = "running" | "success" | "paused" | "failed";

export interface RuntimeNodeModel {
  reasoning_effort: ReasoningEffort;
  current_model: string;
  context_length: number;
  output_length: number;
  thinking: ThinkingMode;
  temperature: number;
}

export interface RuntimeNodeUsage {
  input_tokens: number | null;
  cached_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
}

export interface TurnItem {
  type: string;
  status: "running" | "failed" | "success";
  text?: string;
  [key: string]: unknown;
}

export interface RetryTurnItem extends TurnItem {
  type: "retry";
  event: "model_retry";
  category: "network";
  message: string;
  attempt: number;
  max_retries: number;
  delay_seconds: number;
}

export interface TurnMessage {
  role: "user" | "assistant";
  content: TurnItem[];
  [key: string]: unknown;
}

/** Synthetic Session root. Its wire shape intentionally contains identifiers only. */
export interface RuntimeRootNode {
  session_id: string;
  thread_id: string;
  id: string;
}

/** Canonical executable Turn shared by the API and web reducer. */
export interface RuntimeTurnNode {
  thread_id: string;
  parent_thread_id: string;
  session_id: string;
  parent_session_id: string;
  id: string;
  parent_id: string;
  version: string;
  firstKeptItemSize: number;
  compactionId: string;
  user: string;
  provider_name: string;
  model: RuntimeNodeModel;
  permission_mode: PermissionMode;
  running_mode: ChatMode;
  usage: RuntimeNodeUsage;
  cwd: string;
  project_cwd: string;
  timestamp: string;
  status: RuntimeNodeStatus;
  current_data_idx: number;
  data: TurnMessage[][];
  /** Read-only delivery metadata; canonical Assistant Items remain unchanged. */
  agent_report_statuses?: Record<string, "success" | "failed">;
}

export type RuntimeTreeNode = RuntimeRootNode | RuntimeTurnNode;
export type RuntimeStateNode = RuntimeTurnNode;

export interface TurnTraceToolOrigin {
  kind: "local" | "mcp";
  server?: string;
  tool: string;
}

export interface TurnTraceTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  origin: TurnTraceToolOrigin;
}

export interface TurnTraceContext {
  system_message: string;
  active_skills: Array<Record<string, unknown>>;
  tools: TurnTraceTool[];
  initialized_at: string;
}

export interface TurnTraceItem {
  sequence: number;
  message_idx: number;
  item_idx: number;
  role: "user" | "assistant";
  item: TurnItem;
  completed_at: string;
}

export interface TurnTraceResponse {
  turn: RuntimeStateNode;
  data_idx: number;
  context: TurnTraceContext | null;
  items: TurnTraceItem[];
  last_sequence: number;
}

export type NodeFrameType = "turn.snapshot" | "turn.delta";

export type RuntimeNodePatch = Partial<Omit<RuntimeStateNode,
  "session_id" | "id" | "thread_id" | "parent_session_id" | "parent_id" | "parent_thread_id" | "data"
>>;

export type TurnDeltaOperation =
  | { op: "append_message"; data_idx: number; message_idx: number; message: TurnMessage }
  | { op: "append_item"; data_idx: number; message_idx: number; item_idx: number; item: TurnItem }
  | { op: "append_text"; data_idx: number; message_idx: number; item_idx: number; delta: string }
  | { op: "set_item_status"; data_idx: number; message_idx: number; item_idx: number; status: TurnItem["status"] };

export interface RuntimeNodeSnapshotFrame {
  type: "turn.snapshot";
  revision: 0;
  turn: RuntimeStateNode;
}

export interface RuntimeNodeDeltaFrame {
  type: "turn.delta";
  session_id: string;
  turn_id: string;
  revision: number;
  patch?: RuntimeNodePatch;
  operations?: TurnDeltaOperation[];
  agent_report_statuses?: Record<string, "success" | "failed">;
}

export type RuntimeNodeFrame = RuntimeNodeSnapshotFrame | RuntimeNodeDeltaFrame;

export interface AgentThreadSummary {
  thread_id: string;
  thread_path: string;
  thread_status: "running" | "success" | "paused" | "failed";
  task_result: string;
}

export type AgentThreadStreamEvent = RuntimeNodeFrame
  | { type: "thread.ready"; session_id: string; thread_id: string }
  | {
      type: "turn.terminal";
      session_id: string;
      thread_id: string;
      turn_id: string;
      status: RuntimeNodeStatus;
    };

export interface AgentThreadMessageResponse {
  delivery_id: string;
  accepted: boolean;
  target_state: "running" | "started" | "idle" | "missing";
  turn_id?: string | null;
  background_admission?: string;
}

export interface RuntimeConfigModel {
  reasoning_effort: ReasoningEffort;
  current_model: string;
  context_length: number;
  output_length: number;
  thinking: ThinkingMode;
  temperature: number;
}

export type StreamMessage = RuntimeNodeFrame;
