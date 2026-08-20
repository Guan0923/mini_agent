import { requestJson } from "./request";

export type AutoSaveRule = "idle_5m" | "after_run" | "hourly";

export interface SyncPreferences {
  auto_save_enabled: boolean;
  auto_save_rule: AutoSaveRule;
}

export interface SyncState {
  local_revision: number;
  cloud_revision: number;
  pending_event_count: number;
  status: "local_only" | "dirty" | "syncing" | "synced" | "conflict" | "error";
  last_error: string;
  updated_at: number | null;
}

export interface SyncJob {
  id: string;
  kind: "sync";
  status: "queued" | "running" | "completed" | "complete" | "failed" | "error" | "conflict" | "cancelled";
  phase: string;
  progress: number;
  error: string;
  created_at: number;
  updated_at: number;
  cancel_requested?: boolean;
}

export interface SyncStatus {
  available?: boolean;
  preferences: SyncPreferences;
  state: SyncState;
  job: SyncJob | null;
}

export function getSyncStatus(): Promise<SyncStatus> {
  return requestJson<SyncStatus>("/api/sync/status");
}

export function updateSyncPreferences(preferences: SyncPreferences): Promise<SyncPreferences> {
  return requestJson<SyncPreferences>("/api/sync/preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(preferences),
  });
}

export function syncNow(force = false): Promise<SyncJob> {
  return requestJson<SyncJob>("/api/sync/now", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
}

/** @deprecated Use syncNow; retained only for embedded clients during rollout. */
export const saveToCloud = syncNow;

export function getSyncJob(id: string): Promise<SyncJob> {
  return requestJson<SyncJob>(`/api/sync/jobs/${encodeURIComponent(id)}`);
}

export function cancelSyncJob(id: string): Promise<SyncJob> {
  return requestJson<SyncJob>(`/api/sync/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}
