import type { Dispatch, SetStateAction } from "react";
import {
  archiveSession,
  deleteSession,
  forkTurn,
  getSessionNodes,
  listSessions,
  renameSession,
  restoreSession,
} from "../api";
import type { Conversation, Page } from "../types";
import { withLoadedTurns } from "./conversationProjection";
import { summaryToConversation } from "./storage";

interface ConversationActionsContext {
  conversations: Conversation[];
  activeConversations: Conversation[];
  currentId: string | null;
  ensureSession: (id: string) => Promise<string>;
  updateConversation: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  setConversations: Dispatch<SetStateAction<Conversation[]>>;
  setCurrentId: Dispatch<SetStateAction<string | null>>;
  setPage: Dispatch<SetStateAction<Page>>;
  setActionError: Dispatch<SetStateAction<string | null>>;
}

export function createConversationActions(context: ConversationActionsContext) {
  const {
    conversations,
    activeConversations,
    currentId,
    ensureSession,
    updateConversation,
    setConversations,
    setCurrentId,
    setPage,
    setActionError,
  } = context;

  async function renameConversation(id: string, title: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      const summary = await renameSession(conversation?.threadId ?? sessionId, title);
      updateConversation(id, (current) => summaryToConversation(summary, current));
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      throw error;
    }
  }

  async function archiveConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      const summary = await archiveSession(conversation?.threadId ?? sessionId);
      updateConversation(id, (current) => summaryToConversation(summary, current));
      if (currentId === id) setCurrentId(activeConversations.find((item) => item.id !== id)?.id ?? null);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function deleteConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      const sessionId = await ensureSession(id);
      await deleteSession(conversation?.threadId ?? sessionId);
      setConversations((previous) => previous.filter((item) => item.id !== id));
      if (currentId === id) setCurrentId(null);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function restoreConversation(id: string) {
    setActionError(null);
    try {
      const conversation = conversations.find((item) => item.id === id);
      if (!conversation) return;
      const sessionId = await ensureSession(id);
      const summary = await restoreSession(conversation.threadId ?? sessionId);
      updateConversation(id, (current) => summaryToConversation(summary, current));
      if (!currentId) setCurrentId(conversation.id);
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function forkConversation(id: string, messageId: string) {
    setActionError(null);
    const source = conversations.find((conversation) => conversation.id === id);
    if (!source) return;
    const index = source.messages.findIndex((message) => message.id === messageId);
    if (index < 0 || source.messages[index].role !== "assistant") return;
    try {
      await ensureSession(id);
      const sourceTurnId = source.messages[index].sourceNodeId;
      if (!sourceTurnId) throw new Error("fork requires an assistant Turn");
      const forked = await forkTurn(sourceTurnId);
      const sidebar = forked.sidebar_thread;
      const branch = withLoadedTurns({
        id: sidebar.thread_id,
        clientId: sidebar.thread_id,
        sessionId: sidebar.session_id,
        threadId: sidebar.thread_id,
        activeTurnId: forked.turn.id,
        title: sidebar.title,
        messages: [],
        messagesLoaded: false,
        updatedAt: sidebar.updated_at,
      }, await getSessionNodes(sidebar.session_id));
      setConversations((previous) => [branch, ...previous]);
      setCurrentId(branch.id);
      setPage("chat");
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
    }
  }

  async function rewindConversation(id: string, messageId: string) {
    setActionError(null);
    const source = conversations.find((conversation) => conversation.id === id);
    if (!source) return undefined;
    const index = source.messages.findIndex((message) => message.id === messageId);
    if (index < 0 || source.messages[index].role !== "user") return undefined;
    try {
      const sessionId = await ensureSession(id);
      const turnId = source.messages[index].nodeId;
      if (!turnId) throw new Error("rewind requires a user Turn");
      setCurrentId(id);
      setPage("chat");
      return {
        content: source.messages[index].content,
        sessionId,
        sourceNodeId: turnId,
        rewindTurnId: turnId,
      };
    } catch (error) {
      setActionError(String((error as Error).message ?? error));
      return undefined;
    }
  }

  async function reloadConversation(id: string, preferredActiveTurnId?: string): Promise<void> {
    const conversation = conversations.find((item) => item.id === id);
    if (!conversation) throw new Error("会话不存在");
    const sessionId = await ensureSession(id);
    const nodes = await getSessionNodes(sessionId);
    updateConversation(id, (current) => withLoadedTurns(current, nodes, preferredActiveTurnId));
  }

  async function useSession(sessionId: string): Promise<string> {
    let target = conversations.find((item) => item.threadId === sessionId || item.sessionId === sessionId);
    if (!target) {
      const summaries = await listSessions("active");
      const summary = summaries.find((item) => item.thread_id === sessionId || item.session_id === sessionId);
      if (!summary) throw new Error(`未知会话：${sessionId}`);
      target = summaryToConversation(summary);
      setConversations((previous) => [target!, ...previous]);
    }
    setCurrentId(target.id);
    setPage("chat");
    if (!target.messagesLoaded) {
      const nodes = await getSessionNodes(target.sessionId ?? sessionId);
      setConversations((previous) => previous.map((item) => (
        item.id === target!.id ? withLoadedTurns(item, nodes) : item
      )));
    }
    return target.id;
  }

  return {
    renameConversation,
    archiveConversation,
    deleteConversation,
    restoreConversation,
    forkConversation,
    rewindConversation,
    reloadConversation,
    useSession,
  };
}
