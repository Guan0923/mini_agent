import { requestJson } from "./request";

export type AutoSaveRule = "idle_5m" | "after_run" | "hourly";

export interface SyncPreferences {
  auto_save_enabled: boolean;
  auto_save_rule: AutoSaveRule;
}

export interface SyncState {
  local_revision: number;
  uploaded_revision: number;
  cloud_snapshot_id: string | null;
  status: "local_only" | "dirty" | "saving" | "synced" | "conflict" | "restoring" | "error";
  last_error: string;
  updated_at: number | null;
}

export interface SyncJob {
  id: string;
  kind: "save" | "restore";
  status: "queued" | "running" | "complete" | "failed" | "conflict" | "cancelled";
  phase: string;
  progress: number;
  snapshot_id: string | null;
  error: string;
  created_at: number;
  updated_at: number;
  cancel_requested?: boolean;
}

export interface CloudSnapshot {
  id: string;
  version: number;
  local_revision: number;
  device_id: string;
  archive_size: number;
  chunk_count: number;
  completed_at: string;
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

export function saveToCloud(force = false): Promise<SyncJob> {
  return requestJson<SyncJob>("/api/sync/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
}

export function getSyncJob(id: string): Promise<SyncJob> {
  return requestJson<SyncJob>(`/api/sync/jobs/${encodeURIComponent(id)}`);
}

export function cancelSyncJob(id: string): Promise<SyncJob> {
  return requestJson<SyncJob>(`/api/sync/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
}

export function getCloudSnapshots(): Promise<CloudSnapshot[]> {
  return requestJson<CloudSnapshot[]>("/api/sync/snapshots");
}

export function restoreCloudSnapshot(id: string): Promise<SyncJob> {
  return requestJson<SyncJob>(`/api/sync/snapshots/${encodeURIComponent(id)}/restore`, { method: "POST" });
}
