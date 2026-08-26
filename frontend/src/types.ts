export type Page = "chat" | "trash" | "benchmark";
export type ChatMode = "agent" | "plan";
export type PermissionMode = "read_only" | "workspace_write" | "full_access";
export type DisplayMode = "minimal" | "medium" | "verbose" | "developer";
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

export type FileSource = "project" | "upload";

export interface FileReference {
  source: FileSource;
  path: string;
}

export interface SessionFileInfo {
  source: FileSource;
  path: string;
  name: string;
  size: number;
  mime: string;
  mtime: string;
  is_image: boolean;
}

export interface AuthUser {
  id: string;
  email: string | null;
  kind?: "account" | "guest";
  guest_import?: { guest_id: string; status: "pending"; created_at: number; updated_at: number } | null;
  display_name: string;
  agent_preferences?: string;
}

export interface AuthResponse {
  user: AuthUser;
}

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
export type ThinkingMode = "enable" | "disable";
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
  text?: string;
  [key: string]: unknown;
}

export interface TurnMessage {
  role: "user" | "assistant";
  content: TurnItem[];
  [key: string]: unknown;
}

/** Canonical persisted node shared by API, TUI and the web reducer. */
export interface RuntimeStateNode {
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
  timestamp: string;
  status: RuntimeNodeStatus;
  current_data_idx: number;
  data: TurnMessage[][];
}

export type NodeFrameType = "turn.snapshot" | "turn.delta";

export type RuntimeNodePatch = Partial<Omit<RuntimeStateNode,
  "session_id" | "id" | "thread_id" | "parent_session_id" | "parent_id" | "parent_thread_id" | "data"
>>;

export type TurnDeltaOperation =
  | { op: "append_item"; data_idx: number; item_idx: number; item: TurnItem }
  | { op: "append_text"; data_idx: number; item_idx: number; delta: string };

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
}

export type RuntimeNodeFrame = RuntimeNodeSnapshotFrame | RuntimeNodeDeltaFrame;

export interface SidebarThread {
  thread_id: string;
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
  deleted_at?: string | null;
  title_is_custom: boolean;
}

export interface Metrics {
  duration_ms?: number;
  model_calls?: number;
  tool_calls?: number;
  active_skills?: Array<{ name?: string }>;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  events: ToolEvent[];
  items?: TurnItem[];
  itemVersion?: number;
  compactionNotice?: boolean;
  status?: string;
  metrics?: Metrics;
  error?: string;
  running?: boolean;
  runId?: string;
  /** Durable runtime node id for rewind targets. */
  nodeId?: string;
  sourceNodeId?: string;
  runtimeNodeIds?: string[];
  decision?: DecisionRequest;
  /** Structured file references attached to a user message. */
  references?: FileReference[];
  /** Durable user-message metadata used by the desktop timeline overlay. */
  timelineSeq?: number;
  timelineTime?: number;
  timelineText?: string;
  timelineSource?: "user" | "steering";
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  /** Number of persisted user and assistant messages shown in history. */
  messageCount?: number;
  updatedAt?: string;
  sessionId?: string;
  clientId?: string;
  archivedAt?: string;
  deletedAt?: string;
  messagesLoaded?: boolean;
  lastNodeId?: string;
  runtimeNodes?: RuntimeStateNode[];
  threadId?: string;
  activeTurnId?: string;
  projectId?: string;
  localOnly?: boolean;
  projectAvailable?: boolean;
  /** True when the title was set by the user; automatic first-message titles stay false. */
  titleIsCustom?: boolean;
}

export interface RuntimeConfigModel {
  reasoning_effort: ReasoningEffort;
  current_model: string;
  context_length: number;
  output_length: number;
  thinking: ThinkingMode;
  temperature: number;
}

export interface DecisionOption {
  label: string;
  description: string;
}

export interface DecisionQuestion {
  id: string;
  header?: string;
  question: string;
  options: DecisionOption[];
}

export interface DecisionRequest {
  decision_id: string;
  kind: "tool" | "plan" | "question" | "resume" | "skill";
  message?: string;
  tool?: string;
  arguments?: Record<string, unknown> | string;
  plan?: string;
  goal?: string;
  steps?: string[];
  details?: string;
  questions?: DecisionQuestion[];
  // Skill trust review (kind === "skill").
  skill?: string;
  description?: string;
  project_id?: string;
  workspace_sha256?: string;
  tree_sha256?: string;
  path?: string;
}

export interface TaskInfo {
  name: string;
  capability: string;
  description: string;
  difficulty: string;
  prompt: string;
  budgets: {
    max_tool_calls: number;
  };
  tags: string[];
  source: {
    benchmark: string;
    task_id: string;
    url: string;
    source_revision: string;
    license: string;
    adaptation_notes: string;
  };
  planner_modes: string[];
}

export interface BenchmarkTraceEvent {
  kind: string;
  timestamp: string;
  message: string;
  data: Record<string, unknown>;
}

export interface BenchmarkResult {
  task_name: string;
  capability?: string;
  status?: string;
  score?: number | null;
  final_answer?: string;
  metrics?: Record<string, unknown>;
  verdicts?: Array<Record<string, unknown>>;
  error?: string | null;
  run_id?: string | null;
  passed?: boolean;
  attempt?: number;
  trace: BenchmarkTraceEvent[];
  failure_phase?: string | null;
}

export interface ToolInfo {
  name: string;
  description: string;
}

export interface SkillInfo {
  name: string;
  description: string;
}

export type StreamMessage = RuntimeNodeFrame;
