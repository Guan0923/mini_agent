import { listSessions, type SessionInfo } from "../api";
import { listProjects, type ProjectInfo } from "../api/projects";
import type { Conversation } from "../types";
import { summaryToConversation } from "./storage";

export interface HydratedConversationCatalog {
  conversations: Conversation[];
  projects: ProjectInfo[];
  removedProjects: ProjectInfo[];
}

export async function hydrateConversationCatalog(): Promise<HydratedConversationCatalog> {
  let summaries: SessionInfo[] = [];
  let projects: ProjectInfo[] = [];
  let removedProjects: ProjectInfo[] = [];
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
  } catch {
    // Backend state is authoritative; unavailable data is never replaced by a browser copy.
  }
  const conversations: Conversation[] = summaries
    .filter((summary) => !summary.deleted_at)
    .map((summary) => summaryToConversation(summary));
  return { conversations, projects, removedProjects };
}
