import type { ChatMode, PermissionMode, ReasoningEffort, StreamMessage } from "../types";
import { ApiError, errorFrom, notifyUnauthorized } from "./request";

export interface StreamOptions {
  sessionId?: string;
  sourceNodeId?: string;
  mode?: ChatMode;
  permissionMode?: PermissionMode;
  reasoningEffort?: ReasoningEffort;
}

async function streamEndpoint(
  url: string,
  body: Record<string, unknown>,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<"completed" | "aborted"> {
  let res: Response;
  try {
    res = await fetch(url, {
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
  let sawNodeDelete = false;
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
            if (message.type === "node.delete") sawNodeDelete = true;
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
  if (!terminal && !sawNodeDelete) throw new Error("SSE stream unexpectedly ended before completion");
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
      permission_mode: normalized.permissionMode,
      reasoning_effort: normalized.reasoningEffort,
      interactive: normalized.permissionMode != null,
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
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/sessions/${encodeURIComponent(sessionId)}/resume`,
    { permission_mode: permissionMode, reasoning_effort: reasoningEffort, source_node_id: sourceNodeId },
    onMessage,
    signal,
  );
}
