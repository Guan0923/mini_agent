import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, RuntimeConfigModel, StreamMessage } from "../../types";
import { apiUrl } from "../transport/base";
import { ApiError, errorFrom, jsonBody, requestJson } from "../transport/request";

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
  queuedDelivery?: { deliveryId: string; messageIds: string[] };
  deliveryId?: string;
  onAccepted?: () => void;
}

const terminalPattern = /^<SSE id="([^"]+)" type="(success|network|failed)">([\s\S]*)<\/SSE>$/;
const MAX_STREAM_RECONNECTS = 12;

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
  let lastEventId = "";
  let reconnects = 0;

  const waitToReconnect = async (): Promise<boolean> => {
    if (signal.aborted) return false;
    if (reconnects >= MAX_STREAM_RECONNECTS) return false;
    const delay = Math.min(2_000, 250 * (2 ** Math.min(reconnects, 3)));
    reconnects += 1;
    return new Promise<boolean>((resolve) => {
      const onAbort = () => {
        globalThis.clearTimeout(timer);
        resolve(false);
      };
      const timer = globalThis.setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve(true);
      }, delay);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  };

  for (;;) {
    let response: Response;
    try {
      response = await fetch(apiUrl(url), {
        method: body ? "POST" : "GET",
        cache: "no-store",
        headers: {
          ...(body ? { "Content-Type": "application/json" } : {}),
          ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal,
      });
    } catch (error) {
      if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
      if (await waitToReconnect()) continue;
      throw error;
    }
    if (!response.ok || !response.body) {
      if (response.status === 503 && await waitToReconnect()) continue;
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
          let blockEventId = "";
          const dataLines: string[] = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("id: ")) blockEventId = line.slice(4);
            else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
          }
          if (dataLines.length === 0) continue;
          const payload = dataLines.join("\n");
          const matched = payload.match(terminalPattern);
          if (matched) {
            if (terminal) throw new SseProtocolError("SSE stream contains more than one terminal envelope");
            terminal = matched;
          } else {
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
          if (blockEventId) lastEventId = blockEventId;
        }
        if (next.done) break;
      }
    } catch (error) {
      if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
      if (error instanceof SseProtocolError) throw error;
      if (await waitToReconnect()) continue;
      throw error;
    } finally {
      reader.releaseLock();
    }
    if (signal.aborted) return "aborted";
    if (!terminal) {
      if (lastEventId && await waitToReconnect()) continue;
      throw new SseProtocolError("SSE stream unexpectedly ended before completion");
    }
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
}

export async function streamChat(
  prompt: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions,
): Promise<"completed" | "aborted"> {
  const turnId = options.turnId ?? crypto.randomUUID();
  const body = {
      id: turnId,
      session_id: options.sessionId,
      thread_id: options.threadId ?? options.sessionId,
      parent_id: options.sourceNodeId ?? "",
      ...(options.queuedDelivery
        ? { queued_delivery: { delivery_id: options.queuedDelivery.deliveryId, message_ids: options.queuedDelivery.messageIds } }
        : {
          delivery_id: options.deliveryId,
          message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
        }),
      ...executionConfig(options),
    };
  let acceptedDeliveryId: string;
  try {
    const receipt = await requestJson<{ turn_id: string; delivery_id: string; status: "accepted" }>(
      "/api/turns",
      { ...jsonBody(body), signal },
    );
    acceptedDeliveryId = receipt.delivery_id;
    options.onAccepted?.();
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  }
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream?session_id=${encodeURIComponent(options.sessionId)}&thread_id=${encodeURIComponent(options.threadId ?? options.sessionId)}&delivery_id=${encodeURIComponent(acceptedDeliveryId)}`,
    undefined,
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
  const receipt = await requestJson<{ turn_id: string; delivery_id: string; status: "accepted" }>(
    `/api/turns/${encodeURIComponent(turnId)}/rewind`,
    { ...jsonBody({
      message: { role: "user", content: [{ type: "text", text: prompt, ...(options.references?.length ? { references: options.references } : {}) }] },
      ...executionConfig(options),
    }), signal },
  );
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream?session_id=${encodeURIComponent(options.sessionId)}&delivery_id=${encodeURIComponent(receipt.delivery_id)}`,
    undefined,
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
  await requestJson<{ turn_id: string; status: "accepted" }>(
    `/api/turns/${encodeURIComponent(sourceNodeId)}/resume`,
    { ...jsonBody({
      permission_mode: permissionMode,
      full_access_acknowledged: fullAccessAcknowledged,
      running_mode: mode,
      provider_name: providerName,
      ...(model ? { model } : {}),
    }), signal },
  );
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(sourceNodeId)}/stream?session_id=${encodeURIComponent(sessionId)}`,
    undefined,
    sourceNodeId,
    onMessage,
    signal,
  );
}

export async function streamAttachedTurn(
  turnId: string,
  onMessage: (message: StreamMessage) => void,
  signal: AbortSignal,
  sessionId?: string,
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/turns/${encodeURIComponent(turnId)}/stream${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ""}`,
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
  deliveryId: string,
  messageIds: string[],
): Promise<void> {
  await requestJson(`/api/turns/${encodeURIComponent(turnId)}/steer`, jsonBody({
      delivery_id: deliveryId,
      message_ids: messageIds,
    }));
}
