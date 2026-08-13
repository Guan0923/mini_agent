import type { ChatMode, PermissionMode, ReasoningEffort, RuntimeConfigModel } from "../types";

export interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  providerName?: string;
  model?: RuntimeConfigModel;
  sourceNodeId?: string;
}

export interface ActiveRun {
  controller: AbortController;
  sessionId: string;
}
