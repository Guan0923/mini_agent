import type { Dispatch, SetStateAction } from "react";
import {
  changeProjectPath,
  createProject,
  createProjectSession,
  listProjects,
  removeProject,
  renameProject,
  restoreProject,
  revokeProjectSkillTrust,
  type ProjectInfo,
} from "../api/projects";
import type { Conversation, Page } from "../types";
import { summaryToConversation } from "./storage";

interface ProjectActionsContext {
  projectLoading: boolean;
  activeConversations: Conversation[];
  setProjectLoading: Dispatch<SetStateAction<boolean>>;
  setProjects: Dispatch<SetStateAction<ProjectInfo[]>>;
  setRemovedProjects: Dispatch<SetStateAction<ProjectInfo[]>>;
  setConversations: Dispatch<SetStateAction<Conversation[]>>;
  setCurrentId: Dispatch<SetStateAction<string | null>>;
  setPage: Dispatch<SetStateAction<Page>>;
  setActionError: Dispatch<SetStateAction<string | null>>;
  refreshSessions: () => Promise<void>;
}

export function createProjectActions(context: ProjectActionsContext) {
  const {
    projectLoading,
    activeConversations,
    setProjectLoading,
    setProjects,
    setRemovedProjects,
    setConversations,
    setCurrentId,
    setPage,
    setActionError,
    refreshSessions,
  } = context;

  async function newProject(): Promise<void> {
    if (projectLoading) return;
    setProjectLoading(true);
    setActionError(null);
    try {
      const result = await createProject();
      if (!result) return;
      const conversation = summaryToConversation(result.session, {
        id: result.session.session_id,
        clientId: result.session.client_id ?? result.session.session_id,
        title: result.session.title || "新对话",
        messages: [],
        messagesLoaded: true,
        runtimeNodes: [],
      });
      setProjects((previous) => [result.project, ...previous.filter((item) => item.project_id !== result.project.project_id)]);
      setConversations((previous) => [conversation, ...previous]);
      setCurrentId(conversation.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    } finally {
      setProjectLoading(false);
    }
  }

  async function newProjectConversation(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const existing = activeConversations.find(
        (conversation) => conversation.projectId === projectId
          && conversation.messages.length === 0
          && (conversation.messageCount ?? 0) === 0,
      );
      if (existing) {
        setCurrentId(existing.id);
        setPage("chat");
        return;
      }
      const result = await createProjectSession(projectId, crypto.randomUUID());
      const conversation = summaryToConversation(result.session, {
        id: result.session.session_id,
        clientId: result.session.client_id ?? result.session.session_id,
        title: result.session.title || "新对话",
        messages: [],
        messagesLoaded: true,
        runtimeNodes: [],
      });
      setConversations((previous) => [conversation, ...previous]);
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? result.project : item));
      setCurrentId(conversation.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function removeProjectFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      await removeProject(projectId);
      setProjects((previous) => previous.filter((item) => item.project_id !== projectId));
      setRemovedProjects(await listProjects("removed").catch(() => []));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function renameProjectFromSidebar(projectId: string, name: string): Promise<void> {
    setActionError(null);
    try {
      const updated = await renameProject(projectId, name);
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? updated : item));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function changeProjectPathFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const updated = await changeProjectPath(projectId);
      if (!updated) return;
      setProjects((previous) => previous.map((item) => item.project_id === projectId ? updated : item));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function revokeProjectSkillTrustFromSidebar(projectId: string): Promise<void> {
    setActionError(null);
    try {
      await revokeProjectSkillTrust(projectId);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function restoreProjectFromTrash(projectId: string): Promise<void> {
    setActionError(null);
    try {
      const restored = await restoreProject(projectId);
      setProjects((previous) => [restored, ...previous.filter((item) => item.project_id !== projectId)]);
      setRemovedProjects((previous) => previous.filter((item) => item.project_id !== projectId));
      await refreshSessions();
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  return {
    newProject,
    newProjectConversation,
    removeProjectFromSidebar,
    renameProjectFromSidebar,
    changeProjectPathFromSidebar,
    revokeProjectSkillTrustFromSidebar,
    restoreProjectFromTrash,
  };
}
