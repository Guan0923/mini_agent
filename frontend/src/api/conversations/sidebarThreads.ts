import type { SidebarThread } from "../../types";
import { requestJson } from "../transport/request";

export type SidebarThreadSort = "created_at" | "recent_activity";

export interface SidebarThreadOrderResult {
  ordered_thread_ids: string[];
}

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

export async function updateSidebarThreadOrder(
  projectId: string | null,
  order: { orderedThreadIds: string[] } | { sortBy: SidebarThreadSort },
): Promise<SidebarThreadOrderResult> {
  return requestJson("/api/sidebar-threads/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: projectId,
      ...(order && "orderedThreadIds" in order
        ? { ordered_thread_ids: order.orderedThreadIds }
        : { sort_by: order.sortBy }),
    }),
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
