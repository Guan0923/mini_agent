import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, StreamMessage } from "../types";
import { apiUrl } from "./base";
import { ApiError, errorFrom, notifyUnauthorized } from "./request";

export type RagMode = "off" | "tool" | "forced";

export interface StreamOptions {
  sessionId?: string;
  sourceNodeId?: string;
  mode?: ChatMode;
  permissionMode?: PermissionMode;
  fullAccessAcknowledged?: boolean;
  reasoningEffort?: ReasoningEffort;
  providerName?: string;
  model?: RuntimeConfigModel;
  references?: FileReference[];
  ragMode?: RagMode;
}

async function streamEndpoint(
  url: string,
  body: Record<string, unknown>,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<"completed" | "aborted"> {
  let res: Response;
  try {
    res = await fetch(apiUrl(url), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
      credentials: "include",
    });
  } catch (err) {
    if ((err as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw new Error(String((err as Error).message ?? err));
  }
  if (!res.ok || !res.body) {
    if (res.status === 401) notifyUnauthorized();
    throw new ApiError(res.status, await errorFrom(res));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;
  let sawAssistantDelete = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let index: number;
      while ((index = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            const message = JSON.parse(line.slice(6)) as StreamMessage;
            if (message.type === "done" || message.type === "error") terminal = true;
            if (message.type === "node.delete") {
              const raw = message.node?.data?.message;
              if (raw && typeof raw === "object" && !Array.isArray(raw) && (raw as { role?: unknown }).role === "assistant") {
                const blocks = (raw as { content?: unknown }).content;
                const content = Array.isArray(blocks) ? blocks : [];
                const hasToolCall = content.some(
                  (block) => Boolean(block && typeof block === "object" && !Array.isArray(block) && (block as { type?: unknown }).type === "tool_call"),
                );
                const hasAnswer = content.some(
                  (block) => Boolean(block && typeof block === "object" && !Array.isArray(block) && ["text", "bash"].includes(String((block as { type?: unknown }).type))),
                );
                sawAssistantDelete = sawAssistantDelete || (hasAnswer && !hasToolCall);
              }
            }
            onMessage(message);
          } catch {
            /* ignore malformed frames */
          }
        }
      }
    }
  } catch (err) {
    if ((err as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw new Error(String((err as Error).message ?? err));
  } finally {
    reader.releaseLock();
  }
  if (signal.aborted) return "aborted";
  if (!terminal && !sawAssistantDelete) throw new Error("SSE stream unexpectedly ended before completion");
  return "completed";
}

export async function streamChat(
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions | string = {},
): Promise<"completed" | "aborted"> {
  const normalized = typeof options === "string" ? { sessionId: options } : options;
  return streamEndpoint(
    "/api/chat",
    {
      prompt,
      session_id: normalized.sessionId,
      source_node_id: normalized.sourceNodeId,
      mode: normalized.mode ?? "agent",
      ...(normalized.mode ? { running_mode: normalized.mode } : {}),
      permission_mode: normalized.permissionMode,
      ...(normalized.fullAccessAcknowledged ? { full_access_acknowledged: true } : {}),
      reasoning_effort: normalized.reasoningEffort,
      provider_name: normalized.providerName,
      model: normalized.model,
      references: normalized.references,
      interactive: normalized.permissionMode != null,
      ...(normalized.ragMode && normalized.ragMode !== "off" ? { rag_mode: normalized.ragMode } : {}),
    },
    onMessage,
    signal,
  );
}

export async function streamResume(
  sessionId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  permissionMode: PermissionMode,
  reasoningEffort: ReasoningEffort = "medium",
  sourceNodeId?: string,
  providerName?: string,
  model?: RuntimeConfigModel,
  mode?: ChatMode,
  ragMode: RagMode = "off",
  fullAccessAcknowledged = false,
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/sessions/${encodeURIComponent(sessionId)}/resume`,
    {
      permission_mode: permissionMode,
      ...(fullAccessAcknowledged ? { full_access_acknowledged: true } : {}),
      reasoning_effort: reasoningEffort,
      source_node_id: sourceNodeId,
      provider_name: providerName,
      model,
      ...(mode ? { mode } : {}),
      ...(mode ? { running_mode: mode } : {}),
      ...(ragMode !== "off" ? { rag_mode: ragMode } : {}),
    },
    onMessage,
    signal,
  );
}
