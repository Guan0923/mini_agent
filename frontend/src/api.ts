import type {
  AuthResponse,
  AuthUser,
  ChatMessage,
  ChatMode,
  Conversation,
  DecisionRequest,
  PermissionMode,
  ReasoningEffort,
  SkillInfo,
  StreamMessage,
  TaskInfo,
  ToolInfo,
} from "./types";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

let unauthorizedHandler: (() => void) | null = null;

/** Register the app-level response to an expired/revoked browser session. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

async function errorFrom(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* fall through */
  }
  return `HTTP ${res.status}`;
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, { credentials: "include", ...init });
  if (!res.ok) {
    if (res.status === 401) notifyUnauthorized();
    throw new ApiError(res.status, await errorFrom(res));
  }
  return res.json() as Promise<T>;
}

function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export async function getCurrentUser(): Promise<AuthUser | null> {
  const res = await fetch("/api/auth/me", { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) throw new ApiError(res.status, await errorFrom(res));
  const body = (await res.json()) as AuthResponse | AuthUser;
  return "user" in body ? body.user : body;
}

export interface UserProfile {
  display_name: string;
  agent_preferences: string;
}

export async function getProfile(): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/auth/profile");
}

export async function updateProfile(profile: UserProfile): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/auth/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}
export async function requestRegisterCode(email: string): Promise<void> {
  await requestJson("/api/auth/register/code", jsonBody({ email }));
}

export async function register(email: string, code: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>("/api/auth/register", jsonBody({ email, code, password }));
  return body.user;
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>("/api/auth/login", jsonBody({ email, password }));
  return body.user;
}

export async function requestPasswordResetCode(email: string): Promise<void> {
  await requestJson("/api/auth/password-reset/code", jsonBody({ email }));
}

export async function resetPassword(email: string, code: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>(
    "/api/auth/password-reset/confirm",
    jsonBody({ email, code, password }),
  );
  return body.user;
}

export async function logout(): Promise<void> {
  await requestJson("/api/auth/logout", jsonBody({}));
}

export interface DeviceStart {
  poll_secret: string;
  verification_url: string;
  expires_in: number;
  poll_interval: number;
}

export async function startDeviceAuthorization(): Promise<DeviceStart> {
  return requestJson<DeviceStart>("/api/auth/device/start", jsonBody({}));
}

export async function deviceInfo(grant: string): Promise<{ server_url: string; created_at: number; status: string }> {
  return requestJson(`/api/auth/device/info?grant=${encodeURIComponent(grant)}`);
}

export async function approveDevice(grant: string, approved: boolean): Promise<void> {
  await requestJson("/api/auth/device/approve", jsonBody({ grant, approved }));
}

export async function listTasks(): Promise<TaskInfo[]> {
  return requestJson<TaskInfo[]>("/benchmark/tasks");
}

export async function runBenchmark(task: string, planner: string): Promise<Record<string, unknown>> {
  return requestJson<Record<string, unknown>>("/benchmark/run", jsonBody({ task, planner }));
}

export async function runAllBenchmark(planner: string): Promise<Array<Record<string, unknown>>> {
  return requestJson<Array<Record<string, unknown>>>("/benchmark/run-all", jsonBody({ planner }));
}

export async function listTools(): Promise<ToolInfo[]> {
  return requestJson<ToolInfo[]>("/api/tools");
}

export async function listSkills(): Promise<SkillInfo[]> {
  return requestJson<SkillInfo[]>("/api/skills");
}

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

export interface TimezoneOption {
  identifier: string;
  label: string;
}

export interface TimezoneInfo {
  timezone: string;
  options: TimezoneOption[];
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

interface StreamOptions {
  sessionId?: string;
  mode?: ChatMode;
  permissionMode?: PermissionMode;
  reasoningEffort?: ReasoningEffort;
}

async function streamEndpoint(
  url: string,
  body: Record<string, unknown>,
  onMessage: (m: StreamMessage) => void,
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
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of block.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            const message = JSON.parse(line.slice(6)) as StreamMessage;
            if (message.type === "done" || message.type === "error") terminal = true;
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
  if (!terminal) throw new Error("SSE stream unexpectedly ended before completion");
  return "completed";
}

export async function streamChat(
  prompt: string,
  onMessage: (m: StreamMessage) => void,
  signal: AbortSignal,
  options: StreamOptions | string = {},
): Promise<"completed" | "aborted"> {
  const normalized = typeof options === "string" ? { sessionId: options } : options;
  return streamEndpoint(
    "/api/chat",
    {
      prompt,
      session_id: normalized.sessionId,
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
  onMessage: (m: StreamMessage) => void,
  signal: AbortSignal,
  permissionMode: PermissionMode,
  reasoningEffort: ReasoningEffort = "medium",
): Promise<"completed" | "aborted"> {
  return streamEndpoint(
    `/api/sessions/${encodeURIComponent(sessionId)}/resume`,
    { permission_mode: permissionMode, reasoning_effort: reasoningEffort },
    onMessage,
    signal,
  );
}
