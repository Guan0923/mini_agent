export type Page = "chat" | "trash" | "benchmark";
export type ChatMode = "agent" | "plan";
export type PermissionMode = "read_only" | "workspace_write" | "full_access";
export type DisplayMode = "minimal" | "medium" | "verbose" | "developer";
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";
export type ThinkingMode = "enable" | "disable";

export interface LocalProfile {
  display_name: string;
  agent_preferences: string;
}

export interface ToolInfo {
  name: string;
  description: string;
}

export interface SkillInfo {
  name: string;
  description: string;
}
