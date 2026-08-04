export type Page = "chat" | "benchmark";

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
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
}

export interface TaskInfo {
  name: string;
  capability: string;
  description: string;
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
}
