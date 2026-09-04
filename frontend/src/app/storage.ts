import type { SessionInfo } from "../api";
import type { Conversation } from "../types";

export const STORAGE_KEY = "mini-agent-conversations";
export const ARCHIVE_READ_KEY = "mini-agent-archive-read";
export const BROWSER_STATE_VERSION_KEY = "mini-agent-browser-state-version";
export const BROWSER_STATE_VERSION = "redis-message-transport-v1";
export type ArchiveReadState = Record<string, string>;

const LEGACY_BROWSER_PREFIXES = [STORAGE_KEY];

/**
 * Drop browser-owned conversation/message state. UI-only preferences remain.
 *
 * The backend cannot remove this cache because it lives in the browser.  Run
 * this once per browser profile during the protocol migration; subsequent
 * canonical conversations are always hydrated from the local backend.
 */
export function resetLegacyBrowserState(storage?: Storage): void {
  if (typeof window === "undefined" && !storage) return;
  const target = storage ?? window.localStorage;

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
export function loadArchiveReadState(): ArchiveReadState {
  try {
    const raw = localStorage.getItem(ARCHIVE_READ_KEY);
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
    id: summary.thread_id ?? existing?.id ?? summary.session_id,
    title: summary.title || existing?.title || "新对话",
    messages: existing?.messages ?? [],
    messageCount:
      summary.message_count ??
      (existing?.messages.length
        ? existing.messages.filter((message) => message.role === "user" || message.role === "assistant").length
        : existing?.messageCount ?? 0),
    createdAt: summary.created_at ?? existing?.createdAt,
    updatedAt: summary.conversation_updated_at ?? summary.updated_at ?? existing?.updatedAt,
    lastActivityAt: summary.last_activity_at ?? existing?.lastActivityAt,
    sessionId: summary.session_id,
    threadId: summary.thread_id ?? existing?.threadId ?? summary.session_id,
    clientId: summary.client_id ?? existing?.clientId ?? existing?.id ?? summary.session_id,
    archivedAt: summary.archived_at ?? undefined,
    deletedAt: summary.deleted_at ?? undefined,
    messagesLoaded: existing?.messagesLoaded ?? false,
    lastNodeId: summary.last_node_id !== undefined ? summary.last_node_id ?? undefined : existing?.lastNodeId,
    activeTurnId: existing?.activeTurnId,
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

export function mergeConversationSummaries(
  previous: Conversation[],
  summaries: SessionInfo[],
  runningConversationIds: ReadonlySet<string> = new Set(),
): Conversation[] {
  const byThread = new Map(previous.map((conversation) => [
    conversation.threadId ?? conversation.sessionId ?? conversation.id,
    conversation,
  ]));
  const byClient = new Map(
    previous
      .filter((conversation) => conversation.clientId)
      .map((conversation) => [conversation.clientId!, conversation]),
  );
  return summaries
    .filter((summary) => !summary.deleted_at)
    .map((summary) => {
      const existing = byThread.get(summary.thread_id ?? summary.session_id)
        ?? (summary.client_id ? byClient.get(summary.client_id) : undefined);
      const merged = summaryToConversation(summary, existing);
      if (!existing || !runningConversationIds.has(existing.id)) return merged;
      const existingCount = existing.messageCount ?? 0;
      const mergedCount = merged.messageCount ?? 0;
      const existingTime = existing.updatedAt ? Date.parse(existing.updatedAt) : Number.NaN;
      const mergedTime = merged.updatedAt ? Date.parse(merged.updatedAt) : Number.NaN;
      return {
        ...merged,
        messageCount: Math.max(existingCount, mergedCount),
        updatedAt: Number.isNaN(existingTime) || existingTime <= mergedTime
          ? merged.updatedAt
          : existing.updatedAt,
      };
    });
}
