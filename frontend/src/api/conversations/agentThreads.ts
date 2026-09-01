import type {
  AgentThreadMessageResponse,
  AgentThreadStreamEvent,
  AgentThreadSummary,
  ChatMode,
  FileReference,
  PermissionMode,
  RuntimeConfigModel,
} from "../../types";
import { apiUrl } from "../transport/base";
import { ApiError, errorFrom, requestJson } from "../transport/request";

export async function listAgentThreadChildren(
  sessionId: string,
  threadId: string,
): Promise<AgentThreadSummary[]> {
  return requestJson(
    `/api/agent-threads/${encodeURIComponent(threadId)}/children?session_id=${encodeURIComponent(sessionId)}`,
  );
}

export async function sendAgentThreadMessage(
  targetThreadId: string,
  values: {
    sessionId: string;
    content: string;
    references?: FileReference[];
    mode: ChatMode;
    permissionMode: PermissionMode;
    fullAccessAcknowledged?: boolean;
    providerName?: string;
    model?: RuntimeConfigModel;
  },
): Promise<AgentThreadMessageResponse> {
  return requestJson(`/api/agent-threads/${encodeURIComponent(targetThreadId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: values.sessionId,
      content: values.content,
      references: values.references ?? [],
      running_mode: values.mode,
      permission_mode: values.permissionMode,
      full_access_acknowledged: Boolean(values.fullAccessAcknowledged),
      provider_name: values.providerName,
      model: values.model,
    }),
  });
}

export async function streamAgentThread(
  sessionId: string,
  threadId: string,
  onEvent: (event: AgentThreadStreamEvent) => void,
  signal: AbortSignal,
  lastEventId = "",
  onCursor?: (eventId: string) => void,
): Promise<"aborted" | "ended"> {
  let response: Response;
  try {
    response = await fetch(
      apiUrl(`/api/agent-threads/${encodeURIComponent(threadId)}/stream?session_id=${encodeURIComponent(sessionId)}`),
      {
        signal,
        cache: "no-store",
        headers: lastEventId ? { "Last-Event-ID": lastEventId } : undefined,
      },
    );
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  }
  if (!response.ok || !response.body) throw new ApiError(response.status, await errorFrom(response));

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const next = await reader.read();
      buffer += next.done ? decoder.decode() : decoder.decode(next.value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let blockEventId = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("id: ")) {
            blockEventId = line.slice(4);
            continue;
          }
          if (!line.startsWith("data: ")) continue;
          const payload = JSON.parse(line.slice(6)) as AgentThreadStreamEvent;
          if (!["thread.ready", "turn.snapshot", "turn.delta", "turn.terminal"].includes(payload.type)) {
            throw new Error(`Unsupported Agent Thread SSE event: ${String((payload as { type?: unknown }).type)}`);
          }
          onEvent(payload);
        }
        if (blockEventId) onCursor?.(blockEventId);
      }
      if (next.done) break;
    }
  } catch (error) {
    if ((error as Error).name === "AbortError" || signal.aborted) return "aborted";
    throw error;
  } finally {
    reader.releaseLock();
  }
  return signal.aborted ? "aborted" : "ended";
}
