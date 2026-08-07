import type { ChatMessage } from "../types";
import { jsonBody, requestJson } from "./request";

export interface SessionInfo {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_run_id?: string | null;
  last_run_status: string | null;
  client_id?: string | null;
  archived_at?: string | null;
  deleted_at?: string | null;
}

export interface SessionMessage {
  id?: string;
  run_id?: string | null;
  role: "user" | "assistant";
  content: string;
  events?: ChatMessage["events"];
  status?: string;
  metrics?: ChatMessage["metrics"];
  error?: string;
  running?: boolean;
}

export interface TimezoneInfo {
  timezone: string;
  options: Array<{ identifier: string; label: string }>;
}

export interface ForkableRun {
  run_id: string;
  task: string;
  status: string;
  updated_at: string;
}

export async function listSessions(state: "active" | "archived" | "deleted" | "all" = "active"): Promise<SessionInfo[]> {
  return requestJson<SessionInfo[]>(`/api/sessions?state=${encodeURIComponent(state)}`);
}

export async function createSession(
  title?: string,
  clientId?: string,
  messages: Array<Pick<ChatMessage, "role" | "content">> = [],
): Promise<SessionInfo> {
  return requestJson<SessionInfo>("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title?.trim() || null, client_id: clientId, messages }),
  });
}

export async function renameSession(sessionId: string, title: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function archiveSession(sessionId: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/archive`, { method: "POST" });
}

export async function restoreSession(sessionId: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/restore`, { method: "POST" });
}

export async function deleteSession(sessionId: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export async function forkSession(
  sessionId: string,
  runId: string | undefined,
  title: string,
  clientId: string,
  fallbackMessages: Array<Pick<ChatMessage, "role" | "content">>,
): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId, title, client_id: clientId, fallback_messages: fallbackMessages }),
  });
}

export async function rewindSession(
  sessionId: string,
  runId: string | undefined,
  title: string,
  clientId: string,
  fallbackMessages: Array<Pick<ChatMessage, "role" | "content">>,
): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/rewind`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId, title, client_id: clientId, fallback_messages: fallbackMessages }),
  });
}

export async function getSessionMessages(sessionId: string): Promise<SessionMessage[]> {
  return requestJson<SessionMessage[]>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function getSessionTranscript(sessionId: string): Promise<SessionMessage[]> {
  return requestJson<SessionMessage[]>(`/api/sessions/${encodeURIComponent(sessionId)}/transcript`);
}

export async function getTimezone(sessionId: string): Promise<TimezoneInfo> {
  return requestJson<TimezoneInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/timezone`);
}

export async function setTimezone(sessionId: string, timezone: string): Promise<{ timezone: string }> {
  return requestJson<{ timezone: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/timezone`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone }),
  });
}

export async function compactSession(sessionId: string): Promise<{
  compacted: boolean;
  previous_messages: number;
  remaining_messages: number;
  summary?: string | null;
}> {
  return requestJson(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, { method: "POST" });
}

export async function getTrace(sessionId: string): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>(`/api/sessions/${encodeURIComponent(sessionId)}/trace`);
}

export async function listForkableRuns(): Promise<ForkableRun[]> {
  return requestJson<ForkableRun[]>("/api/forkable-runs");
}

export async function forkRun(runId: string): Promise<SessionInfo> {
  return requestJson<SessionInfo>(`/api/runs/${encodeURIComponent(runId)}/fork`, { method: "POST" });
}

export async function submitDecision(
  decisionId: string,
  choice: string,
  options: { supplement?: string; answers?: Record<string, string[]> } = {},
): Promise<void> {
  await requestJson("/api/decisions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision_id: decisionId, choice, ...options }),
  });
}
