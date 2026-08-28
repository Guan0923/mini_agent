import { requestJson } from "../transport/request";
import type { SessionInfo } from "../conversations/sessions";

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
  const response = await fetch("/api/projects", { method: "POST" });
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

export async function renameProject(projectId: string, name: string): Promise<ProjectInfo> {
  return requestJson<ProjectInfo>(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function changeProjectPath(projectId: string): Promise<ProjectInfo | null> {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/path`, {
    method: "POST",
  });
  if (response.status === 204) return null;
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail || `请求失败（${response.status}）`);
  return response.json() as Promise<ProjectInfo>;
}

export async function restoreProject(projectId: string): Promise<ProjectInfo> {
  return requestJson<ProjectInfo>(`/api/projects/${encodeURIComponent(projectId)}/restore`, { method: "POST" });
}

export interface ProjectSkillTrustDetails {
  project_id: string;
  workspace_sha256: string;
  trusted_skills: Record<string, { tree_sha256: string }>;
}

export async function getProjectSkillTrust(projectId: string): Promise<ProjectSkillTrustDetails> {
  return requestJson<ProjectSkillTrustDetails>(`/api/projects/${encodeURIComponent(projectId)}/skill-trust`);
}

export async function revokeProjectSkillTrust(projectId: string): Promise<ProjectSkillTrustDetails> {
  return requestJson<ProjectSkillTrustDetails>(`/api/projects/${encodeURIComponent(projectId)}/skill-trust`, {
    method: "DELETE",
  });
}
