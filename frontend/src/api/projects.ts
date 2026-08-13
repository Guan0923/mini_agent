import { requestJson } from "./request";
import type { SessionInfo } from "./sessions";

export interface ProjectInfo {
  id?: string;
  project_id: string;
  name: string;
  cwd: string;
  available: boolean;
  created_at: string;
  updated_at: string;
  removed_at?: string | null;
  conversation_count: number;
  session_ids?: string[];
}

export interface ProjectCreateResult {
  project: ProjectInfo;
  session: SessionInfo;
}

export async function listProjects(state: "active" | "removed" | "all" = "active"): Promise<ProjectInfo[]> {
  return requestJson<ProjectInfo[]>(`/api/projects?state=${encodeURIComponent(state)}`);
}

export async function createProject(): Promise<ProjectCreateResult | null> {
  const response = await fetch("/api/projects", { method: "POST", credentials: "include" });
  if (response.status === 204) return null;
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `请求失败（${response.status}）`);
  return response.json() as Promise<ProjectCreateResult>;
}

export async function createProjectSession(projectId: string, clientId?: string): Promise<ProjectCreateResult> {
  return requestJson<ProjectCreateResult>(`/api/projects/${encodeURIComponent(projectId)}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "新对话", client_id: clientId }),
  });
}

export async function removeProject(projectId: string): Promise<ProjectInfo> {
  return requestJson<ProjectInfo>(`/api/projects/${encodeURIComponent(projectId)}/remove`, { method: "POST" });
}

export async function restoreProject(projectId: string): Promise<ProjectInfo> {
  return requestJson<ProjectInfo>(`/api/projects/${encodeURIComponent(projectId)}/restore`, { method: "POST" });
}
