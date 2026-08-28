import type { RuntimeStateNode, RuntimeTreeNode, SidebarThread } from "../../types";
import { isRuntimeTurnNode, normalizeRuntimeNode } from "../../app/runtime/runtimeNodeNormalization";
import { requestJson } from "../transport/request";
import { archiveSidebarThread, createSidebarThread, deleteSidebarThread, listSidebarThreads, renameSidebarThread, restoreSidebarThread } from "./sidebarThreads";
import { listTurns } from "./turns";

export interface SessionInfo {
  session_id: string;
  thread_id?: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_run_status: string | null;
  client_id?: string | null;
  archived_at?: string | null;
  deleted_at?: string | null;
  last_node_id?: string | null;
  local_only?: boolean;
  title_is_custom?: boolean;
  project_id?: string | null;
  project_available?: boolean | null;
}

export interface TimezoneInfo { timezone: string; options: Array<{ identifier: string; label: string }>; }
function summary(item: SidebarThread): SessionInfo {
  return {
    session_id: item.session_id,
    thread_id: item.thread_id,
    title: item.title,
    created_at: item.created_at,
    updated_at: item.updated_at,
    message_count: 0,
    last_run_status: null,
    archived_at: item.archived_at,
    deleted_at: item.deleted_at,
    title_is_custom: item.title_is_custom,
  };
}

export async function listSessions(state: "active" | "archived" | "deleted" | "all" = "active"): Promise<SessionInfo[]> {
  return (await listSidebarThreads(state)).map(summary);
}

export async function createSession(title = "新对话", clientId?: string): Promise<SessionInfo> {
  return summary(await createSidebarThread(title, clientId));
}

export async function renameSession(threadId: string, title: string): Promise<SessionInfo> {
  return summary(await renameSidebarThread(threadId, title));
}

export async function archiveSession(threadId: string): Promise<SessionInfo> {
  return summary(await archiveSidebarThread(threadId));
}

export async function restoreSession(threadId: string): Promise<SessionInfo> {
  return summary(await restoreSidebarThread(threadId));
}

export async function deleteSession(threadId: string): Promise<SessionInfo> {
  return summary(await deleteSidebarThread(threadId));
}

export async function getSessionNodes(sessionId: string): Promise<RuntimeTreeNode[]> {
  return (await listTurns(sessionId)).map(normalizeRuntimeNode);
}

export async function getSessionLeaves(sessionId: string): Promise<RuntimeStateNode[]> {
  const nodes = (await getSessionNodes(sessionId)).filter(isRuntimeTurnNode);
  const parents = new Set(nodes.map((item) => `${item.parent_session_id}:${item.parent_id}`));
  return nodes.filter((item) => !parents.has(`${item.session_id}:${item.id}`));
}

export async function patchRuntimeConfig(
  _sessionId: string,
  values: { node_id: string; provider_name?: string; model?: Record<string, unknown>; permission_mode?: "read_only" | "workspace_write" | "full_access"; full_access_acknowledged?: boolean; running_mode?: "agent" | "plan" },
): Promise<RuntimeStateNode> {
  const { node_id, ...body } = values;
  const node = await requestJson<RuntimeStateNode>(`/api/turns/${encodeURIComponent(node_id)}/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return normalizeRuntimeNode(node);
}

export async function getTimezone(_sessionId: string): Promise<TimezoneInfo> {
  return { timezone: "Asia/Shanghai", options: [] };
}

export async function setTimezone(_sessionId: string, timezone: string): Promise<{ timezone: string }> {
  return { timezone };
}

export async function submitDecision(decisionId: string, choice: string, options: { supplement?: string; answers?: Record<string, string[]> } = {}): Promise<void> {
  await requestJson("/api/decisions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision_id: decisionId, choice, ...options }),
  });
}
