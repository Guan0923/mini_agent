import type { AuthResponse, AuthUser, SkillInfo, StreamMessage, TaskInfo, ToolInfo } from "./types";

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
  last_run_status: string | null;
}

export async function listSessions(): Promise<SessionInfo[]> {
  return requestJson<SessionInfo[]>("/api/sessions");
}

export interface SessionMessage {
  role: string;
  content: string;
}

export async function getSessionMessages(sessionId: string): Promise<SessionMessage[]> {
  return requestJson<SessionMessage[]>(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
}

export async function streamChat(
  prompt: string,
  onMessage: (m: StreamMessage) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
      signal,
      credentials: "include",
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onMessage({ type: "error", error: String((err as Error).message ?? err) });
    return;
  }
  if (!res.ok || !res.body) {
    if (res.status === 401) notifyUnauthorized();
    onMessage({ type: "error", error: await errorFrom(res) });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
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
            onMessage(JSON.parse(line.slice(6)) as StreamMessage);
          } catch {
            /* ignore malformed frames */
          }
        }
      }
    }
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onMessage({ type: "error", error: String((err as Error).message ?? err) });
  }
}
