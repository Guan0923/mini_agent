import { streamChat, streamResume } from "../api";
import type { ChatMessage, StreamMessage } from "../types";
import type { ActiveRun, ChatRunRequest } from "./types";

export interface RunControllerCallbacks {
  activeRuns: Map<string, ActiveRun>;
  updateLastMessage: (conversationId: string, updater: (message: ChatMessage) => ChatMessage) => void;
  rebindRunSession: (conversationId: string, sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
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

    const onMessage = (message: StreamMessage) => {
      const active = callbacks.activeRuns.get(request.conversationId);
      if (active?.controller !== controller || controller.signal.aborted) return;
      if (message.type === "event") {
        const kind = message.kind ?? "";
        if (kind === "response_delta") {
          const content = (message.data?.content as string | undefined) ?? message.message ?? "";
          if (content) callbacks.updateLastMessage(request.conversationId, (item) => ({ ...item, content: item.content + content }));
        } else if (kind === "tool_call" || kind === "tool_result" || kind === "tool_failed") {
          callbacks.updateLastMessage(request.conversationId, (item) => ({
            ...item,
            events: [...item.events, { kind, message: message.message ?? "", data: message.data }],
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
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          content: message.final_answer ?? "",
          status: message.status,
          metrics: message.metrics,
          running: false,
          decision: undefined,
          runId: message.run_id,
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
        ? await streamResume(request.sessionId, onMessage, controller.signal, request.permissionMode, request.reasoningEffort)
        : await streamChat(request.prompt ?? "", onMessage, controller.signal, {
            sessionId: request.sessionId,
            mode: request.mode,
            permissionMode: request.permissionMode,
            reasoningEffort: request.reasoningEffort,
          });
      if (result === "aborted") {
        callbacks.updateLastMessage(request.conversationId, (item) => ({
          ...item,
          running: false,
          status: "已停止",
          decision: undefined,
        }));
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
      if (active?.controller === controller) callbacks.activeRuns.delete(request.conversationId);
    }
  }

  function stopConversation(id: string): void {
    const active = callbacks.activeRuns.get(id);
    if (!active) return;
    active.controller.abort();
    callbacks.updateLastMessage(id, (item) => ({ ...item, running: false, status: "已停止", decision: undefined }));
  }

  return { runConversation, stopConversation };
}
