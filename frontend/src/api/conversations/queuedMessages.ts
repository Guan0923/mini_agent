import type { QueuedMessage } from "../../app/types";
import type { FileReference } from "../../types";
import { apiUrl } from "../transport/base";
import { ApiError, errorFrom, requestJson } from "../transport/request";

function queueUrl(threadId: string, messageId?: string): string {
  const base = `/api/sidebar-threads/${encodeURIComponent(threadId)}/queued-messages`;
  return messageId ? `${base}/${encodeURIComponent(messageId)}` : base;
}

export async function listQueuedMessages(threadId: string): Promise<QueuedMessage[]> {
  return requestJson(queueUrl(threadId));
}

export async function createQueuedMessage(
  threadId: string,
  id: string,
  content: string,
  references: FileReference[] = [],
): Promise<QueuedMessage> {
  return requestJson(queueUrl(threadId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, content, references }),
  });
}

export async function updateQueuedMessage(
  threadId: string,
  messageId: string,
  content: string,
  references: FileReference[] = [],
): Promise<QueuedMessage> {
  return requestJson(queueUrl(threadId, messageId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, references }),
  });
}

export async function deleteQueuedMessage(threadId: string, messageId: string): Promise<void> {
  const response = await fetch(apiUrl(queueUrl(threadId, messageId)), { method: "DELETE" });
  if (!response.ok) throw new ApiError(response.status, await errorFrom(response));
}
