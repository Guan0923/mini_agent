import type { RuntimeStateNode, RuntimeTreeNode, SidebarThread } from "../types";
import { requestJson } from "./request";

export async function listTurns(sessionId: string): Promise<RuntimeTreeNode[]> {
  return requestJson(`/api/turns?session_id=${encodeURIComponent(sessionId)}`);
}

export async function patchTurnCurrentData(turnId: string, currentDataIdx: number): Promise<RuntimeStateNode> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/current-data`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_data_idx: currentDataIdx }),
  });
}

export async function forkTurn(
  turnId: string,
  title?: string,
): Promise<{ turn: RuntimeStateNode; sidebar_thread: SidebarThread }> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function compactTurn(turnId: string): Promise<RuntimeStateNode> {
  return requestJson(`/api/turns/${encodeURIComponent(turnId)}/compact`, {
    method: "POST",
  });
}
