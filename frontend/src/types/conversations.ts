import type { DecisionRequest } from "./decisions";
import type { FileReference } from "./files";
import type { RuntimeTreeNode, ToolEvent, TurnItem } from "./runtime";

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
  deliveryId?: string;
  pending?: boolean;
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
  runtimeNodes?: RuntimeTreeNode[];
  threadId?: string;
  activeTurnId?: string;
  projectId?: string;
  localOnly?: boolean;
  projectAvailable?: boolean;
  /** True when the title was set by the user; automatic first-message titles stay false. */
  titleIsCustom?: boolean;
  /** Hide canonical ancestry through this copied Turn in a right-panel side chat. */
  hiddenBeforeTurnId?: string;
}
