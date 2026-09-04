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
    const [allSessions, activeProjects, removedProjectItems] = await Promise.all([
      listSessions("all").catch(() => []),
      listProjects("active").catch(() => []),
      listProjects("removed").catch(() => []),
    ]);
    summaries = allSessions;
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
