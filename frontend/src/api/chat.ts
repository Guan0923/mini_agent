import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, StreamMessage } from "../types";
import { apiUrl } from "./base";
import { ApiError, errorFrom, notifyUnauthorized } from "./request";
import { requestJson } from "./request";

export type RagMode = "off" | "tool" | "forced";

export interface StreamOptions {
  sessionId: string;
  threadId?: string;
  turnId?: string;
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

export interface QueuedTurnMessage {
  content: string;
  references?: FileReference[];
}

const terminalPattern = /^<SSE id="([^"]+)" type="(success|network|failed)">([\s\S]*)<\/SSE>$/;

function executionConfig(options: StreamOptions): Record<string, unknown> {
  return {
    permission_mode: options.permissionMode ?? "read_only",
    full_access_acknowledged: Boolean(options.fullAccessAcknowledged),
    running_mode: options.mode ?? "agent",
    provider_name: options.providerName,
    model: options.model,
  };
}

async function streamEndpoint(
  url: string,
  body: Record<string, unknown>,
  expectedTurnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<"completed" | "aborted"> {
  let response: Response;
  try {
    response = await fetch(apiUrl(url), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
      credentials: "include",
    });
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  }
  if (!response.ok || !response.body) {
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, await errorFrom(response));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal: RegExpMatchArray | null = null;
  try {
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      buffer += decoder.decode(next.value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6);
          const matched = payload.match(terminalPattern);
          if (matched) {
            terminal = matched;
            continue;
          }
          const frame = JSON.parse(payload) as StreamMessage;
          if (frame.type !== "turn.create" && frame.type !== "turn.update") {
            throw new Error(`Unsupported SSE frame: ${String((frame as { type?: unknown }).type)}`);
          }
          onMessage(frame);
        }
      }
    }
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  } finally {
    reader.releaseLock();
  }
  if (signal.aborted) return "aborted";
  if (!terminal) throw new Error("SSE stream unexpectedly ended before completion");
  if (terminal[1] !== expectedTurnId) {
    throw new Error("SSE terminal id does not match the active Turn");
  }
  if (terminal[2] === "network") throw new Error("network");
  if (terminal[2] === "failed") throw new Error(terminal[3] || "Turn failed");
  return "completed";
}

export async function streamChat(
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<"completed" | "aborted"> {
  const turnId = options.turnId ?? crypto.randomUUID();
  return streamEndpoint(
    "/api/turns",
    {
      id: turnId,
      session_id: options.sessionId,
      thread_id: options.threadId ?? options.sessionId,
      parent_id: options.sourceNodeId ?? "",
      message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
      ...executionConfig(options),
    },
    turnId,
    onMessage,
    signal,
  );
}

export async function streamRewind(
  turnId: string,
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/rewind`,
    {
      message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
      ...executionConfig(options),
    },
    turnId,
    onMessage,
    signal,
  );
}

export async function streamResume(
  sessionId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  permissionMode: PermissionMode,
  _reasoningEffort: ReasoningEffort = "medium",
  sourceNodeId?: string,
  providerName?: string,
  model?: RuntimeConfigModel,
  mode: ChatMode = "agent",
  _ragMode: RagMode = "off",
  fullAccessAcknowledged = false,
): Promise<"completed" | "aborted"> {
  if (!sourceNodeId) throw new Error("resume requires a Turn id");
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(sourceNodeId)}/resume`,
    {
      permission_mode: permissionMode,
      full_access_acknowledged: fullAccessAcknowledged,
      running_mode: mode,
      provider_name: providerName,
      ...(model ? { model } : {}),
    },
    sourceNodeId,
    onMessage,
    signal,
  );
}

export async function streamQueuedTurns(
  messages: QueuedTurnMessage[],
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<"completed" | "aborted"> {
  let parent = options.sourceNodeId;
  for (const message of messages) {
    const turnId = crypto.randomUUID();
    const result = await streamChat(message.content, (frame) => {
      parent = frame.turn.id;
      onMessage(frame);
    }, signal, { ...options, turnId, sourceNodeId: parent, references: message.references });
    if (result === "aborted") return result;
  }
  return "completed";
}

export async function pauseTurn(turnId: string): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/pause`, { method: "POST" });
}
