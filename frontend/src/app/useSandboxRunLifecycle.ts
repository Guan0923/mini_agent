import { useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { getSessionNodes, pauseTurn } from "../api";
import type { Conversation, RuntimeStateNode } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";
import { withLoadedTurns } from "./conversationProjection";
import { isRuntimeTurnNode } from "./runtime/runtimeNodeNormalization";
import type { SandboxHealthState } from "./useSandboxHealth";

interface UseSandboxRunLifecycleOptions {
  sandboxHealth: Pick<SandboxHealthState, "phase">;
  activeRunsRef: MutableRefObject<Map<string, ActiveRun>>;
  conversations: Conversation[];
  panelConversations: Record<string, Conversation>;
  current: Conversation | null;
  setConversations: Dispatch<SetStateAction<Conversation[]>>;
  setPanelConversations: Dispatch<SetStateAction<Record<string, Conversation>>>;
  updateConversation: (id: string, updater: (conversation: Conversation) => Conversation) => void;
  runConversation: (request: ChatRunRequest) => Promise<void>;
}

export function useSandboxRunLifecycle({
  sandboxHealth,
  activeRunsRef,
  conversations,
  panelConversations,
  current,
  setConversations,
  setPanelConversations,
  updateConversation,
  runConversation,
}: UseSandboxRunLifecycleOptions): void {
  const pausedForSandboxOutageRef = useRef(new Set<string>());

  useEffect(() => {
    if (sandboxHealth.phase === "healthy") {
      pausedForSandboxOutageRef.current.clear();
      return;
    }
    if (sandboxHealth.phase !== "unhealthy") return;
    const runningTurnIds = new Set<string>();
    const turnSessions = new Map<string, { conversationId: string; sessionId: string }>();
    for (const active of activeRunsRef.current.values()) {
      if (active.turnId) runningTurnIds.add(active.turnId);
    }
    for (const conversation of [...conversations, ...Object.values(panelConversations)]) {
      for (const node of conversation.runtimeNodes ?? []) {
        if (isRuntimeTurnNode(node) && node.status === "running") {
          runningTurnIds.add(node.id);
          if (conversation.sessionId) {
            turnSessions.set(node.id, { conversationId: conversation.id, sessionId: conversation.sessionId });
          }
        }
      }
    }
    if (turnSessions.size > 0) {
      setConversations((previous) => previous.map((conversation) => {
        const nodes = conversation.runtimeNodes;
        if (!nodes?.some((node) => runningTurnIds.has(node.id))) return conversation;
        return withLoadedTurns(conversation, nodes.map((node) => (
          isRuntimeTurnNode(node) && runningTurnIds.has(node.id) ? { ...node, status: "paused" } : node
        )));
      }));
      setPanelConversations((previous) => Object.fromEntries(Object.entries(previous).map(([id, conversation]) => {
        const nodes = conversation.runtimeNodes;
        if (!nodes?.some((node) => runningTurnIds.has(node.id))) return [id, conversation];
        return [id, withLoadedTurns(conversation, nodes.map((node) => (
          isRuntimeTurnNode(node) && runningTurnIds.has(node.id) ? { ...node, status: "paused" } : node
        )))];
      })));
    }
    for (const turnId of runningTurnIds) {
      if (pausedForSandboxOutageRef.current.has(turnId)) continue;
      pausedForSandboxOutageRef.current.add(turnId);
      void pauseTurn(turnId).catch(async () => {
        pausedForSandboxOutageRef.current.delete(turnId);
        const target = turnSessions.get(turnId);
        if (!target) return;
        try {
          const nodes = await getSessionNodes(target.sessionId);
          updateConversation(target.conversationId, (conversation) => withLoadedTurns(conversation, nodes));
        } catch {
          // The next health transition or session reload retries reconciliation.
        }
      });
    }
  }, [conversations, panelConversations, sandboxHealth.phase]);

  useEffect(() => {
    if (
      sandboxHealth.phase !== "healthy"
      || !current?.sessionId
      || !current.runtimeNodes
      || activeRunsRef.current.has(current.id)
    ) return;
    const activeTurn = current.runtimeNodes.find(
      (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
        && node.id === current.activeTurnId
        && node.status === "running",
    );
    if (!activeTurn) return;
    void runConversation({
      conversationId: current.id,
      sessionId: current.sessionId,
      threadId: activeTurn.thread_id,
      turnId: activeTurn.id,
      prompt: null,
      resume: false,
      attach: true,
      mode: activeTurn.running_mode,
      permissionMode: activeTurn.permission_mode,
      reasoningEffort: activeTurn.model.reasoning_effort,
      providerName: activeTurn.provider_name,
      model: activeTurn.model,
      sourceNodeId: activeTurn.id,
    });
  }, [current?.id, current?.sessionId, current?.activeTurnId, current?.runtimeNodes, sandboxHealth.phase]);

  useEffect(() => {
    if (sandboxHealth.phase !== "healthy") return;
    for (const conversation of Object.values(panelConversations)) {
      if (!conversation.sessionId || activeRunsRef.current.has(conversation.id)) continue;
      const activeTurn = conversation.runtimeNodes?.find(
        (node): node is RuntimeStateNode => isRuntimeTurnNode(node)
          && node.id === conversation.activeTurnId
          && node.thread_id === conversation.threadId
          && node.status === "running",
      );
      if (!activeTurn) continue;
      void runConversation({
        conversationId: conversation.id,
        sessionId: conversation.sessionId,
        threadId: activeTurn.thread_id,
        turnId: activeTurn.id,
        prompt: null,
        resume: false,
        attach: true,
        mode: activeTurn.running_mode,
        permissionMode: activeTurn.permission_mode,
        reasoningEffort: activeTurn.model.reasoning_effort,
        providerName: activeTurn.provider_name,
        model: activeTurn.model,
        sourceNodeId: activeTurn.id,
      });
    }
  }, [panelConversations, sandboxHealth.phase]);
}
