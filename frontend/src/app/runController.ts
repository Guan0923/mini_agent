import { pauseTurn, streamChat, streamQueuedTurns, streamResume, streamRewind } from "../api";
import type { ChatMessage, RuntimeNodeFrame, StreamMessage } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";
import { integrateRuntimeNodeFrame, projectRuntimeNode } from "./runtimeDetailProjection";

export interface RunControllerCallbacks {
  activeRuns: Map<string, ActiveRun>;
  updateLastMessage: (conversationId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  rebindRunSession: (conversationId: string, sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  applyRuntimeNodeFrame?: (frame: RuntimeNodeFrame) => void;
  updateConversation?: (conversationId: string, updater: (conversation: import("../types").Conversation) => import("../types").Conversation) => void;
}

export function createRunController(callbacks: RunControllerCallbacks) {
  async function runConversation(request: ChatRunRequest): Promise<void> {
    if (callbacks.activeRuns.has(request.conversationId)) return;
    const controller = new AbortController();
    const active: ActiveRun = { controller, sessionId: request.sessionId, turnId: request.turnId };
    callbacks.activeRuns.set(request.conversationId, active);
    let finalTurn: StreamMessage["turn"] | undefined;

    const onMessage = (message: StreamMessage) => {
      if (callbacks.activeRuns.get(request.conversationId)?.controller !== controller) return;
      const frame: RuntimeNodeFrame = { type: message.type, turn: message.turn };
      active.turnId = message.turn.id;
      finalTurn = message.turn;
      callbacks.applyRuntimeNodeFrame?.(frame);
      callbacks.updateConversation?.(request.conversationId, (conversation) => integrateRuntimeNodeFrame(conversation, frame));
      if (active.stopRequested && !active.cancelIssued) {
        active.cancelIssued = true;
        void pauseTurn(message.turn.id).catch(() => undefined);
      }
    };

    const options = {
      sessionId: request.sessionId,
      threadId: request.threadId ?? request.sessionId,
      turnId: request.turnId,
      sourceNodeId: request.sourceNodeId,
      mode: request.mode,
      permissionMode: request.permissionMode,
      fullAccessAcknowledged: request.permissionMode === "full_access",
      reasoningEffort: request.reasoningEffort,
      providerName: request.providerName,
      model: request.model,
      references: request.references,
    } as const;

    try {
      const result = request.resume
        ? await streamResume(
            request.sessionId,
            onMessage,
            controller.signal,
            request.permissionMode,
            request.reasoningEffort,
            request.sourceNodeId,
            request.providerName,
            request.model,
            request.mode,
            request.permissionMode === "full_access",
          )
        : request.rewindTurnId
          ? await streamRewind(request.rewindTurnId, request.prompt ?? "", onMessage, controller.signal, options)
          : request.queuedTurns
            ? await streamQueuedTurns(request.queuedTurns, onMessage, controller.signal, options)
            : await streamChat(request.prompt ?? "", onMessage, controller.signal, options);
      if (result === "aborted") return;
      if (finalTurn) {
        const projection = projectRuntimeNode(finalTurn);
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: projection.content || item.content,
          error: projection.error,
          status: finalTurn?.status,
          running: false,
          decision: undefined,
        }));
      }
      await callbacks.refreshSessions().catch(() => undefined);
    } catch (error) {
      if (!controller.signal.aborted) {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: String((error as Error).message ?? error),
          running: finalTurn?.status === "running",
          decision: undefined,
        }));
      }
    } finally {
      if (callbacks.activeRuns.get(request.conversationId)?.controller === controller) {
        callbacks.activeRuns.delete(request.conversationId);
      }
    }
  }

  function stopConversation(id: string): void {
    const active = callbacks.activeRuns.get(id);
    if (!active || active.stopRequested) return;
    active.stopRequested = true;
    if (active.turnId) {
      active.cancelIssued = true;
      void pauseTurn(active.turnId).catch(() => undefined);
    }
  }

  return { runConversation, stopConversation };
}
