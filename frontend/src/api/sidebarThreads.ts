import type { SidebarThread } from "../types";
import { requestJson } from "./request";

export async function listSidebarThreads(state: "active" | "archived" | "deleted" | "all" = "active"): Promise<SidebarThread[]> {
  return requestJson(`/api/sidebar-threads?state=${encodeURIComponent(state)}`);
}

export async function createSidebarThread(title = "新对话", clientId?: string): Promise<SidebarThread> {
  return requestJson("/api/sidebar-threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title, client_id: clientId }),
  });
}

export async function renameSidebarThread(threadId: string, title: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function archiveSidebarThread(threadId: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}/archive`, { method: "POST" });
}

export async function restoreSidebarThread(threadId: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}/restore`, { method: "POST" });
}

export async function deleteSidebarThread(threadId: string): Promise<SidebarThread> {
  return requestJson(`/api/sidebar-threads/${encodeURIComponent(threadId)}`, { method: "DELETE" });
}
