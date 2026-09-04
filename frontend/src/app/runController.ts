import { pauseTurn, SseProtocolError, streamAttachedTurn, streamChat, streamResume, streamRewind } from "../api";
import type { ChatMessage, RuntimeStateNode, StreamMessage } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";
import { integrateRuntimeNodeUpdates, projectRuntimeNode } from "./runtime/runtimeDetailProjection";
import { applyRuntimeNodeFrame, runtimeNodeAccumulator } from "./runtime/runtimeNodeReducer";

export interface RunControllerCallbacks {
  activeRuns: Map<string, ActiveRun>;
  updateLastMessage: (conversationId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  rebindRunSession: (conversationId: string, sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  updateConversation?: (conversationId: string, updater: (conversation: import("../types").Conversation) => import("../types").Conversation) => void;
  recoverConversation: (conversationId: string, sessionId: string, turnId?: string) => Promise<void>;
  checkSandboxHealth?: () => Promise<unknown>;
}

export function createRunController(callbacks: RunControllerCallbacks) {
  async function runConversation(request: ChatRunRequest): Promise<void> {
    const previous = callbacks.activeRuns.get(request.conversationId);
    if (previous) {
      if (!request.waitForActiveRun) return;
      await previous.settled;
      if (callbacks.activeRuns.has(request.conversationId)) return;
    }
    const controller = new AbortController();
    let releaseSettled!: () => void;
    const settled = new Promise<void>((resolve) => {
      releaseSettled = resolve;
    });
    const active: ActiveRun = { controller, sessionId: request.sessionId, turnId: request.turnId, settled };
    callbacks.activeRuns.set(request.conversationId, active);
    const accumulator = runtimeNodeAccumulator();
    const pendingTurns = new Map<string, RuntimeStateNode>();
    let finalTurn: RuntimeStateNode | undefined;
    let admissionAccepted = false;
    let pendingActiveTurnId: string | undefined;
    let forcePathProjection = false;
    let scheduledFrame: number | undefined;
    let scheduledWithAnimationFrame = false;

    const cancelScheduledFrame = () => {
      if (scheduledFrame === undefined) return;
      if (scheduledWithAnimationFrame && typeof globalThis.cancelAnimationFrame === "function") {
        globalThis.cancelAnimationFrame(scheduledFrame);
      } else {
        globalThis.clearTimeout(scheduledFrame);
      }
      scheduledFrame = undefined;
      scheduledWithAnimationFrame = false;
    };

    const flushPendingFrames = () => {
      cancelScheduledFrame();
      if (!pendingActiveTurnId || pendingTurns.size === 0) return;
      const turns = [...pendingTurns.values()];
      const activeTurnId = pendingActiveTurnId;
      const reproject = forcePathProjection;
      pendingTurns.clear();
      pendingActiveTurnId = undefined;
      forcePathProjection = false;
      callbacks.updateConversation?.(
        request.conversationId,
        (conversation) => integrateRuntimeNodeUpdates(conversation, turns, activeTurnId, reproject),
      );
    };

    const scheduleFrameFlush = () => {
      if (scheduledFrame !== undefined) return;
      if (typeof globalThis.requestAnimationFrame === "function") {
        scheduledWithAnimationFrame = true;
        scheduledFrame = globalThis.requestAnimationFrame(() => {
          scheduledFrame = undefined;
          scheduledWithAnimationFrame = false;
          flushPendingFrames();
        });
      } else {
        scheduledFrame = globalThis.setTimeout(() => {
          scheduledFrame = undefined;
          flushPendingFrames();
        }, 0);
      }
    };

    const onMessage = (message: StreamMessage) => {
      if (callbacks.activeRuns.get(request.conversationId)?.controller !== controller) return;
      let turn: RuntimeStateNode;
      try {
        if (message.type === "turn.snapshot") {
          // Every Redis-backed reconnect begins with a fresh SQLite
          // authority snapshot. Rebase this Turn before applying subsequent
          // connection-local revisions; other continuation Turns keep their
          // own accumulators.
          const key = `${message.turn.session_id}:${message.turn.id}`;
          accumulator.nodes.delete(key);
          accumulator.revisions.delete(key);
        }
        turn = applyRuntimeNodeFrame(accumulator, message);
      } catch (error) {
        throw new SseProtocolError(String((error as Error).message ?? error));
      }
      const key = `${turn.session_id}:${turn.id}`;
      finalTurn = turn;
      active.turnId = turn.id;
      if (message.type === "turn.snapshot") request.onBaseline?.(turn);
      pendingTurns.set(key, turn);
      pendingActiveTurnId = turn.id;
      forcePathProjection ||= message.type === "turn.snapshot"
        || message.patch?.current_data_idx !== undefined
        || message.operations?.some((operation) => operation.op === "append_message") === true;
      scheduleFrameFlush();
      if (active.stopRequested && !active.cancelIssued) {
        active.cancelIssued = true;
        void pauseTurn(turn.id).catch(() => undefined);
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
      queuedDelivery: request.queuedDelivery,
      deliveryId: request.deliveryId,
      onAccepted: () => {
        admissionAccepted = true;
        request.onAccepted?.();
      },
    } as const;

    try {
      const result = request.attach
        ? await streamAttachedTurn(
          request.turnId ?? request.sourceNodeId ?? "",
          onMessage,
          controller.signal,
          request.sessionId,
        )
        : request.resume
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
            : await streamChat(request.prompt ?? "", onMessage, controller.signal, options);
      flushPendingFrames();
      if (result === "aborted") return;
      if (result === "silent_failed") {
        const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
        await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: undefined,
          status: "failed",
          running: false,
          decision: undefined,
        }));
        return;
      }
      if (request.attach && finalTurn?.status === "running") {
        await callbacks.recoverConversation(
          request.conversationId,
          request.sessionId,
          finalTurn.id,
        ).catch(() => undefined);
        return;
      }
      if (finalTurn) {
        const projection = projectRuntimeNode(finalTurn);
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: projection.content || item.content,
          error: projection.errorSuppressed ? undefined : projection.error,
          status: finalTurn?.status,
          running: false,
          decision: undefined,
        }));
      }
    } catch (error) {
      flushPendingFrames();
      if (!admissionAccepted) request.onAdmissionRejected?.();
      if (!finalTurn && !request.attach && !controller.signal.aborted) {
        await callbacks.checkSandboxHealth?.().catch(() => undefined);
      }
      const protocolError = error instanceof SseProtocolError;
      if (protocolError) {
        controller.abort();
        const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
        if (recoveryTurnId && !request.attach) await pauseTurn(recoveryTurnId).catch(() => undefined);
        await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
      }
      if (request.attach) {
        if (!protocolError) {
          const recoveryTurnId = active.turnId ?? request.turnId ?? request.sourceNodeId;
          await callbacks.recoverConversation(request.conversationId, request.sessionId, recoveryTurnId).catch(() => undefined);
        }
        return;
      }
      if (protocolError || !controller.signal.aborted) {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: String((error as Error).message ?? error),
          running: protocolError ? false : finalTurn?.status === "running",
          decision: undefined,
        }));
      }
    } finally {
      cancelScheduledFrame();
      if (callbacks.activeRuns.get(request.conversationId)?.controller === controller) {
        callbacks.activeRuns.delete(request.conversationId);
      }
      await callbacks.refreshSessions().catch(() => undefined);
      releaseSettled();
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
