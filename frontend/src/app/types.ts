import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, RuntimeStateNode } from "../types";
export interface ChatRunRequest {
  conversationId: string;
  sessionId: string;
  threadId?: string;
  turnId?: string;
  prompt: string | null;
  resume: boolean;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  providerName?: string;
  model?: RuntimeConfigModel;
  sourceNodeId?: string;
  rewindTurnId?: string;
  references?: FileReference[];
  attach?: boolean;
  waitForActiveRun?: boolean;
  onBaseline?: (turn: RuntimeStateNode) => void;
}

export interface ActiveRun {
  controller: AbortController;
  sessionId: string;
  turnId?: string;
  settled: Promise<void>;
  stopRequested?: boolean;
  cancelIssued?: boolean;
  cancelTimer?: ReturnType<typeof setTimeout>;
}

export interface QueuedMessage {
  id: string;
  content: string;
  references?: import("../types").FileReference[];
  sendingSteeringId?: string;
  sendingTurnId?: string;
}
