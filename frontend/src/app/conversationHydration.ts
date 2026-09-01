import { createSession, listSessions, type SessionInfo } from "../api";
import { listProjects, type ProjectInfo } from "../api/projects";
import type { Conversation } from "../types";
import { loadConversations, summaryToConversation } from "./storage";

export interface HydratedConversationCatalog {
  conversations: Conversation[];
  projects: ProjectInfo[];
  removedProjects: ProjectInfo[];
}

export async function hydrateConversationCatalog(storageKey: string): Promise<HydratedConversationCatalog> {
  const local = loadConversations(storageKey);
  let summaries: SessionInfo[] = [];
  let projects: ProjectInfo[] = [];
  let removedProjects: ProjectInfo[] = [];
  let projectSessionIds = new Set<string>();
  try {
    const [active, archived, deleted, activeProjects, removedProjectItems] = await Promise.all([
      listSessions("active").catch(() => []),
      listSessions("archived").catch(() => []),
      listSessions("deleted").catch(() => []),
      listProjects("active").catch(() => []),
      listProjects("removed").catch(() => []),
    ]);
    summaries = [...active, ...archived, ...deleted];
    projects = activeProjects;
    removedProjects = removedProjectItems;
    projectSessionIds = new Set(
      [...activeProjects, ...removedProjectItems].flatMap((project) => project.session_ids ?? []),
    );
  } catch {
    // The local cache remains usable while the backend is unavailable.
  }

  // The project index is authoritative for membership. Keep stale project
  // sessions from being re-imported as ordinary local conversations.
  const byClient = new Map<string, SessionInfo>();
  const bySession = new Map<string, SessionInfo>();
  const deletedByClient = new Map<string, SessionInfo>();
  const deletedBySession = new Map<string, SessionInfo>();
  for (const summary of summaries) {
    bySession.set(summary.thread_id ?? summary.session_id, summary);
    if (summary.deleted_at) {
      deletedBySession.set(summary.thread_id ?? summary.session_id, summary);
      if (summary.client_id) deletedByClient.set(summary.client_id, summary);
    } else if (summary.client_id) {
      byClient.set(summary.client_id, summary);
    }
  }

  const conversations: Conversation[] = [];
  for (const conversation of local) {
    const exactDeleted = deletedBySession.get(conversation.threadId ?? conversation.sessionId ?? conversation.id);
    if (exactDeleted) continue;
    const summary =
      bySession.get(conversation.threadId ?? conversation.sessionId ?? conversation.id) ??
      byClient.get(conversation.clientId ?? conversation.id);
    if (summary) {
      if (summary.deleted_at) continue;
      conversations.push(summaryToConversation(summary, conversation));
      bySession.delete(summary.thread_id ?? summary.session_id);
      continue;
    }
    if (deletedByClient.has(conversation.clientId ?? conversation.id)) continue;
    if (conversation.sessionId && projectSessionIds.has(conversation.sessionId)) continue;
    if (conversation.projectId || conversation.localOnly) continue;

    if (conversation.messages.length > 0) {
      try {
        const imported = await createSession(conversation.title, conversation.clientId ?? conversation.id);
        conversations.push(summaryToConversation(imported, conversation));
        bySession.delete(imported.thread_id ?? imported.session_id);
        continue;
      } catch {
        // Preserve the legacy conversation and retry on the next operation.
      }
    }
    conversations.push(conversation);
  }

  for (const summary of bySession.values()) {
    if (!summary.deleted_at) conversations.push(summaryToConversation(summary));
  }
  return { conversations, projects, removedProjects };
}
