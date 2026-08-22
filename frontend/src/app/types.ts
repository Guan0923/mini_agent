import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel } from "../types";
import type { RagMode } from "../api/chat";

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
  sourceNodeSessionId?: string;
  branch?: boolean;
  references?: FileReference[];
  batchMessages?: Array<{ content: string; references?: FileReference[] }>;
  ragMode?: RagMode;
}

export interface ActiveRun {
  controller: AbortController;
  sessionId: string;
  jobId?: string;
  stopRequested?: boolean;
  cancelIssued?: boolean;
  cancelTimer?: ReturnType<typeof setTimeout>;
}

export interface QueuedMessage {
  id: string;
  content: string;
  references?: import("../types").FileReference[];
}
