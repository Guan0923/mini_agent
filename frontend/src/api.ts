import type { SkillInfo, StreamMessage, TaskInfo, ToolInfo } from "./types";

async function errorFrom(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch {
    /* fall through */
  }
  return `HTTP ${res.status}`;
}

export async function listTasks(): Promise<TaskInfo[]> {
  const res = await fetch("/benchmark/tasks");
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
}

export async function runBenchmark(task: string, planner: string): Promise<Record<string, unknown>> {
  const res = await fetch("/benchmark/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task, planner }),
  });
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
}

export async function runAllBenchmark(planner: string): Promise<Array<Record<string, unknown>>> {
  const res = await fetch("/benchmark/run-all", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ planner }),
  });
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
}

export async function listTools(): Promise<ToolInfo[]> {
  const res = await fetch("/api/tools");
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
}

export async function listSkills(): Promise<SkillInfo[]> {
  const res = await fetch("/api/skills");
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
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
  const res = await fetch("/api/sessions");
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
}

export interface SessionMessage {
  role: string;
  content: string;
}

export async function getSessionMessages(sessionId: string): Promise<SessionMessage[]> {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  if (!res.ok) throw new Error(await errorFrom(res));
  return res.json();
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
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onMessage({ type: "error", error: String((err as Error).message ?? err) });
    return;
  }
  if (!res.ok || !res.body) {
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
