import type { ChatMode, PermissionMode, ReasoningEffort } from "../types";

export interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  sourceNodeId?: string;
}

export interface ActiveRun {
  controller: AbortController;
  sessionId: string;
}
