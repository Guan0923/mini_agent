import { streamChat, streamResume } from "../api";
import { cancelJob } from "../api";
import type { ChatMessage, RuntimeNodeFrame, StreamMessage } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";
import { appendLegacyRuntimeEvent, integrateRuntimeNodeFrame, projectRuntimeNode } from "./runtimeDetailProjection";
import { applyRunSegment } from "./runSegmentReducer";

export interface RunControllerCallbacks {
  activeRuns: Map<string, ActiveRun>;
  updateLastMessage: (conversationId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  rebindRunSession: (conversationId: string, sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  applyRuntimeNodeFrame?: (frame: RuntimeNodeFrame) => void;
  updateConversation?: (conversationId: string, updater: (conversation: import("../types").Conversation) => import("../types").Conversation) => void;
}

/** Build the streaming runner while keeping transport concerns outside the UI. */
export function createRunController(callbacks: RunControllerCallbacks) {
  async function runConversation(request: ChatRunRequest): Promise<void> {
    if (callbacks.activeRuns.has(request.conversationId)) {
      callbacks.updateLastMessage(request.conversationId, (item) => ({
        ...item,
        running: false,
        error: "上一运行仍在停止，请稍后再试。",
        decision: undefined,
      }));
      return;
    }
    const controller = new AbortController();
    callbacks.activeRuns.set(request.conversationId, { controller, sessionId: request.sessionId });
    let nodeProtocol = false;
    let sawDone = false;
    let finalNode: RuntimeNodeFrame["node"] | undefined;

    const onMessage = (message: StreamMessage) => {
      const active = callbacks.activeRuns.get(request.conversationId);
      if (active?.controller !== controller || controller.signal.aborted) return;
      if (message.type === "job" && message.job_id) {
        if (active?.controller === controller) active.jobId = message.job_id;
        if (active?.stopRequested) void cancelJob(message.job_id).catch(() => undefined);
        return;
      } else if ((message.type === "node.create" || message.type === "node.update" || message.type === "node.delete") && message.node) {
        if (active?.stopRequested) return;
        nodeProtocol = true;
        const frame: RuntimeNodeFrame = { type: message.type, node: message.node };
        callbacks.applyRuntimeNodeFrame?.(frame);
        callbacks.updateConversation?.(request.conversationId, (conversation) => integrateRuntimeNodeFrame(conversation, frame));
        if (message.type === "node.delete") finalNode = message.node;
      } else if (message.type === "run_segment") {
        if (active?.stopRequested) return;
        const segment = message.segment;
        if (segment) callbacks.updateLastMessage(request.conversationId, (item) => applyRunSegment(item, segment));
        const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
        if (runId) callbacks.updateLastMessage(request.conversationId, (item) => ({ ...item, runId }));
      } else if (message.type === "event") {
        if (active?.stopRequested) return;
        const kind = message.kind ?? "";
        if (kind === "response_delta" && !nodeProtocol) {
          const content = (message.data?.content as string | undefined) ?? message.message ?? "";
          if (content) callbacks.updateLastMessage(request.conversationId, (item) => ({ ...item, content: item.content + content }));
        } else if (kind.startsWith("thinking_") || kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
          callbacks.updateLastMessage(request.conversationId, (item) => appendLegacyRuntimeEvent(item, {
            kind,
            message: message.message ?? "",
            data: message.data,
          }));
        } else if (kind === "decision_requested" && message.data) {
          callbacks.updateLastMessage(request.conversationId, (item) => ({
            ...item,
            decision: { ...message.data, message: message.message } as ChatMessage["decision"],
          }));
        } else if (kind === "run_finished") {
          callbacks.updateLastMessage(request.conversationId, (item) => ({ ...item, status: message.message }));
        }
        const runId = message.run_id ?? (typeof message.data?.run_id === "string" ? message.data.run_id : undefined);
        if (runId) callbacks.updateLastMessage(request.conversationId, (item) => ({ ...item, runId }));
      } else if (message.type === "done") {
        sawDone = true;
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: message.final_answer ?? "",
          status: message.status,
          metrics: message.metrics,
          ...(message.status === "completed" || message.status === "success" ? { error: undefined } : {}),
          running: false,
          decision: undefined,
          runId: message.run_id ?? item.runId,
        }));
        if (message.session_id && message.session_id !== request.sessionId) {
          void callbacks.rebindRunSession(request.conversationId, message.session_id);
        }
        void callbacks.refreshSessions().catch(() => undefined);
      } else if (message.type === "error") {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: message.error ?? message.message ?? "发生错误",
          running: false,
          decision: undefined,
        }));
      }
    };

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
            "off",
            false,
            request.sourceNodeSessionId,
          )
        : await streamChat(request.prompt ?? "", onMessage, controller.signal, {
            sessionId: request.sessionId,
            sourceNodeId: request.sourceNodeId,
            sourceNodeSessionId: request.sourceNodeSessionId,
            branch: request.branch,
            mode: request.mode,
            permissionMode: request.permissionMode,
            reasoningEffort: request.reasoningEffort,
            providerName: request.providerName,
            model: request.model,
            references: request.references,
          });
      if (result === "aborted") {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          running: false,
          status: "已停止",
          error: "The run was aborted at the user's request.",
          decision: undefined,
        }));
      } else if (!sawDone && finalNode) {
        const terminalNode = finalNode;
        const projection = projectRuntimeNode(terminalNode);
        const content = projection?.content ?? "";
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          status: terminalNode.status,
          content: content || item.content,
          error: projection?.error,
          running: false,
          decision: undefined,
        }));
        void callbacks.refreshSessions().catch(() => undefined);
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          error: String((error as Error).message ?? error),
          running: false,
          decision: undefined,
        }));
      }
    } finally {
      const active = callbacks.activeRuns.get(request.conversationId);
      if (active?.controller === controller) {
        if (active.cancelTimer) clearTimeout(active.cancelTimer);
        callbacks.activeRuns.delete(request.conversationId);
      }
    }
  }

  function stopConversation(id: string): void {
    const active = callbacks.activeRuns.get(id);
    if (!active) return;
    active.stopRequested = true;
    if (active.jobId) {
      void cancelJob(active.jobId).catch(() => undefined);
      // Keep the SSE open until the backend emits its terminal frame. This
      // prevents a second request from racing the still-running job.
      active.cancelTimer = setTimeout(() => active.controller.abort(), 2000);
    } else {
      active.cancelTimer = setTimeout(() => active.controller.abort(), 2000);
    }
    callbacks.updateLastMessage(id, (item) => ({
      ...item,
      running: true,
      status: "已停止",
      error: "The run was aborted at the user's request.",
      decision: undefined,
    }));
  }

  return { runConversation, stopConversation };
}
