export type Page = "chat" | "trash" | "benchmark";
export type ChatMode = "agent" | "plan";
export type PermissionMode = "approval_for_me" | "full_access";
export type DisplayMode = "minimal" | "medium" | "verbose";
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

export interface AuthUser {
  id: string;
  email: string;
  legacy_owner: boolean;
}

export interface AuthResponse {
  user: AuthUser;
}

export interface ToolEvent {
  kind: string;
  message: string;
  data?: Record<string, unknown>;
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
  status?: string;
  metrics?: Metrics;
  error?: string;
  running?: boolean;
  runId?: string;
  decision?: DecisionRequest;
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
  sessionId?: string;
  clientId?: string;
  archivedAt?: string;
  deletedAt?: string;
  messagesLoaded?: boolean;
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
  kind: "tool" | "plan" | "question" | "resume";
  message?: string;
  tool?: string;
  arguments?: Record<string, unknown> | string;
  plan?: string;
  goal?: string;
  steps?: string[];
  details?: string;
  questions?: DecisionQuestion[];
}

export interface TaskInfo {
  name: string;
  capability: string;
  description: string;
  difficulty: string;
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

export interface ToolInfo {
  name: string;
  description: string;
}

export interface SkillInfo {
  name: string;
  description: string;
}

export interface StreamMessage {
  type: "event" | "done" | "error";
  kind?: string;
  message?: string;
  data?: Record<string, unknown>;
  status?: string;
  final_answer?: string;
  metrics?: Metrics;
  error?: string;
  session_id?: string;
  run_id?: string;
  mode?: ChatMode;
}
