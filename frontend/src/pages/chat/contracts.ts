import type { ProviderConfig } from "../../api";
import type { QueuedMessage } from "../../app/types";
import type {
  ChatMode,
  Conversation,
  DisplayMode,
  FileReference,
  Page,
  PermissionMode,
  ReasoningEffort,
  RuntimeNodeModel,
  RuntimeStateNode,
} from "../../types";
import type { ComposerActionMode } from "./Composer";
import type { SandboxHealthState } from "../../app/useSandboxHealth";

export interface ChatPageProps {
  conversation: Conversation | null;
  agentThreadNavigation?: boolean;
  displayMode?: DisplayMode;
  providerConfig?: ProviderConfig | null;
  mode?: ChatMode;
  onModeChange?: (mode: ChatMode) => void;
  onUpdate: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  onNew: (title?: string) => Promise<string> | string;
  onNavigate: (page: Page) => void;
  onEnsureSession?: (id: string) => Promise<string>;
  onFork?: (conversationId: string, messageId: string) => Promise<void>;
  onRewind?: (conversationId: string, messageId: string) => Promise<RewindResult | string | undefined>;
  onSelectSession?: (id: string) => Promise<string>;
  onReload?: (id: string, preferredActiveTurnId?: string) => Promise<void>;
  onRefresh?: () => Promise<void>;
  running?: boolean;
  onRun?: (request: ChatRunRequest) => Promise<void>;
  onStopRun?: (conversationId: string) => void;
  queuedMessages?: QueuedMessage[];
  onQueuedMessagesChange?: (conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) => void;
  onQueuedMessagesRefresh?: (conversationId: string) => Promise<void>;
  sandboxHealth?: Pick<SandboxHealthState, "phase" | "detail">;
}

export interface RewindResult {
  content: string;
  sessionId: string;
  threadId?: string;
  turnId?: string;
  sourceNodeId?: string;
  rewindTurnId?: string;
}

export interface PendingUpload {
  uid: string;
  name: string;
  isImage: boolean;
  status: "uploading" | "done" | "error";
  percent: number;
  file?: File;
  path?: string;
  error?: string;
}

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
  model?: RuntimeNodeModel;
  sourceNodeId?: string;
  rewindTurnId?: string;
  references?: FileReference[];
  waitForActiveRun?: boolean;
  onBaseline?: (turn: RuntimeStateNode) => void;
  queuedDelivery?: { deliveryId: string; messageIds: string[] };
}

export function composerAction(
  status: RuntimeStateNode["status"] | undefined,
  hasDraft: boolean,
  uploading = false,
): { mode: ComposerActionMode; disabled: boolean } {
  const mode: ComposerActionMode = status === "running" && !hasDraft
    ? "pause"
    : status === "paused" && !hasDraft
      ? "resume"
      : "send";
  return { mode, disabled: uploading || (mode === "send" && !hasDraft) };
}
