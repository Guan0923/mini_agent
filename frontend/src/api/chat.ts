import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, StreamMessage } from "../types";
import { apiUrl } from "./base";
import { ApiError, errorFrom, jsonBody, notifyUnauthorized } from "./request";
import { requestJson } from "./request";

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
}

const terminalPattern = /^<SSE id="([^"]+)" type="(success|network|failed)">([\s\S]*)<\/SSE>$/;

export class SseProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SseProtocolError";
  }
}

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
  body: Record<string, unknown> | undefined,
  expectedTurnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<"completed" | "aborted"> {
  let response: Response;
  try {
    response = await fetch(apiUrl(url), {
      method: body ? "POST" : "GET",
      ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
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
  let receivedFrame = false;
  try {
    for (;;) {
      const next = await reader.read();
      if (next.done) {
        buffer += decoder.decode().replace(/\r\n/g, "\n");
      } else {
        buffer += decoder.decode(next.value, { stream: true }).replace(/\r\n/g, "\n");
      }
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6);
          const matched = payload.match(terminalPattern);
          if (matched) {
            if (terminal) throw new SseProtocolError("SSE stream contains more than one terminal envelope");
            terminal = matched;
            continue;
          }
          if (terminal) throw new SseProtocolError("SSE frame arrived after the terminal envelope");
          let frame: StreamMessage;
          try {
            frame = JSON.parse(payload) as StreamMessage;
          } catch (error) {
            throw new SseProtocolError(`Invalid SSE JSON: ${String((error as Error).message ?? error)}`);
          }
          if (frame.type !== "turn.snapshot" && frame.type !== "turn.delta") {
            throw new SseProtocolError(`Unsupported SSE frame: ${String((frame as { type?: unknown }).type)}`);
          }
          if (!receivedFrame) {
            if (frame.type !== "turn.snapshot") throw new SseProtocolError("SSE stream must begin with a Turn snapshot");
            if (frame.turn.id !== expectedTurnId) {
              throw new SseProtocolError("SSE baseline id does not match the requested Turn");
            }
          }
          receivedFrame = true;
          onMessage(frame);
        }
      }
      if (next.done) break;
    }
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  } finally {
    reader.releaseLock();
  }
  if (signal.aborted) return "aborted";
  if (!terminal) throw new SseProtocolError("SSE stream unexpectedly ended before completion");
  if (terminal[1] !== expectedTurnId) {
    throw new SseProtocolError("SSE terminal id does not match the active Turn");
  }
  if (!receivedFrame) {
    if (terminal[2] === "failed") throw new Error(terminal[3] || "Turn failed");
    throw new SseProtocolError("SSE stream completed without a Turn baseline");
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

export async function streamAttachedTurn(
  turnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream`,
    undefined,
    turnId,
    onMessage,
    signal,
  );
}

export async function pauseTurn(turnId: string): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/pause`, { method: "POST" });
}

export async function steerTurn(
  turnId: string,
  steeringId: string,
  content: string,
  references?: FileReference[],
): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/steer`, jsonBody({
      steering_id: steeringId,
      message: {
        role: "user",
        content: [{ type: "text", text: content, ...(references?.length ? { references } : {}) }],
      },
    }));
}
