import type { SessionInfo, SessionMessage } from "../api";
import type { ChatMessage, Conversation } from "../types";
import { normalizeRuntimeNode } from "./runtimeNodeNormalization";

export const STORAGE_KEY = "mini-agent-conversations";
export const ARCHIVE_READ_KEY = "mini-agent-archive-read";
export const BROWSER_STATE_VERSION_KEY = "mini-agent-browser-state-version";
export const BROWSER_STATE_VERSION = "runtime-state-tree-4";
export type ArchiveReadState = Record<string, string>;

const LEGACY_BROWSER_PREFIXES = [STORAGE_KEY, ARCHIVE_READ_KEY, "mini-agent-session-modes"];

/**
 * Drop browser-only state written by the pre-RuntimeState message flow.
 *
 * The backend cannot remove this cache because it lives in the browser.  Run
 * this once per browser profile during the protocol migration; subsequent
 * conversations continue using the same keys and are persisted normally.
 */
export function resetLegacyBrowserState(storage?: Storage): void {
  if (typeof window === "undefined" && !storage) return;
  const target = storage ?? window.localStorage;
  if (target.getItem(BROWSER_STATE_VERSION_KEY) === BROWSER_STATE_VERSION) return;

  const keysToRemove: string[] = [];
  for (let index = 0; index < target.length; index += 1) {
    const key = target.key(index);
    if (key && LEGACY_BROWSER_PREFIXES.some((prefix) => key === prefix || key.startsWith(`${prefix}:`))) {
      keysToRemove.push(key);
    }
  }
  for (const key of keysToRemove) target.removeItem(key);
  target.setItem(BROWSER_STATE_VERSION_KEY, BROWSER_STATE_VERSION);
}

export function loadConversations(key: string): Conversation[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((value): value is Conversation => {
        if (!value || typeof value !== "object") return false;
        const candidate = value as Partial<Conversation>;
        return typeof candidate.id === "string" && Array.isArray(candidate.messages);
      })
      .map((conversation) => ({
        ...conversation,
        clientId: conversation.clientId ?? conversation.id,
        messageCount:
          conversation.messages.length > 0
            ? conversation.messages.filter((message) => message.role === "user" || message.role === "assistant").length
            : conversation.messageCount ?? 0,
        messagesLoaded: conversation.messagesLoaded ?? conversation.messages.length > 0,
        runtimeNodes: conversation.runtimeNodes?.map(normalizeRuntimeNode),
        messages: conversation.messages.map((message) =>
          message.running
            ? { ...message, running: false, status: message.status ?? "上次运行已中断" }
            : message,
        ),
      }));
  } catch {
    return [];
  }
}

export function loadArchiveReadState(userId: string | undefined): ArchiveReadState {
  if (!userId) return {};
  try {
    const raw = localStorage.getItem(`${ARCHIVE_READ_KEY}:${userId}`);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
    );
  } catch {
    return {};
  }
}

export function markArchivedAsRead(state: ArchiveReadState, conversations: Conversation[]): ArchiveReadState {
  let changed = false;
  const next = { ...state };
  for (const conversation of conversations) {
    if (!conversation.archivedAt || next[conversation.id] === conversation.archivedAt) continue;
    next[conversation.id] = conversation.archivedAt;
    changed = true;
  }
  return changed ? next : state;
}

export function countUnreadArchived(conversations: Conversation[], state: ArchiveReadState): number {
  return conversations.filter((conversation) => state[conversation.id] !== conversation.archivedAt).length;
}

export function summaryToConversation(summary: SessionInfo, existing?: Conversation): Conversation {
  return {
    id: existing?.id ?? summary.client_id ?? summary.session_id,
    title: summary.title || existing?.title || "新对话",
    messages: existing?.messages ?? [],
    messageCount:
      summary.message_count ??
      (existing?.messages.length
        ? existing.messages.filter((message) => message.role === "user" || message.role === "assistant").length
        : existing?.messageCount ?? 0),
    updatedAt: summary.updated_at ?? existing?.updatedAt,
    sessionId: summary.session_id,
    clientId: summary.client_id ?? existing?.clientId ?? existing?.id ?? summary.session_id,
    archivedAt: summary.archived_at ?? undefined,
    deletedAt: summary.deleted_at ?? undefined,
    messagesLoaded: existing?.messagesLoaded ?? false,
    lastNodeId: summary.last_node_id !== undefined ? summary.last_node_id ?? undefined : existing?.lastNodeId,
    runtimeNodes: existing?.runtimeNodes,
    // When the API explicitly returns null, it is authoritative: clear a
    // stale browser-only project binding instead of hiding an ordinary
    // conversation under a project group.  Only an omitted field preserves
    // compatibility with older backends.
    projectId: summary.project_id !== undefined ? summary.project_id ?? undefined : existing?.projectId,
    localOnly: summary.local_only !== undefined ? summary.local_only : existing?.localOnly,
    projectAvailable: summary.project_available !== undefined ? summary.project_available ?? undefined : existing?.projectAvailable,
    titleIsCustom: summary.title_is_custom !== undefined ? summary.title_is_custom : existing?.titleIsCustom,
  };
}

export function transcriptToMessages(transcript: SessionMessage[]): ChatMessage[] {
  return transcript.map((message, index) => ({
    id: message.id ?? `transcript-${index}`,
    role: message.role,
    content: message.content,
    events: message.events ?? [],
    status: message.status,
    metrics: message.metrics,
    error: message.error,
    running: message.running ? false : undefined,
    runId: message.run_id ?? undefined,
    sourceNodeId: message.source_node_id ?? undefined,
    references: message.references,
  }));
}

export function importableMessages(messages: ChatMessage[]): Array<Pick<ChatMessage, "role" | "content">> {
  return messages
    .filter((message) => message.content.trim() || message.role === "user")
    .map(({ role, content }) => ({ role, content }));
}
