import { requestJson, jsonBody } from "../transport/request";

export type JobKind = "subprocess" | "thread" | "service";
export type JobLane = "foreground" | "background" | "service";
export type JobState = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface JobInfo {
  id: string;
  kind: JobKind;
  lane: JobLane;
  state: JobState;
  health?: "unknown" | "healthy" | "degraded" | "down" | null;
  started_at: string | null;
  finished_at: string | null;
  queued_at: string | null;
  admitted_at: string | null;
  error: string | null;
  exit_code: number | null;
  cancel_requested: boolean;
  cancellable: boolean;
}

export interface JobQuery {
  state?: JobState;
  lane?: JobLane;
  session_id?: string;
}

export function listJobs(query: JobQuery = {}): Promise<JobInfo[]> {
  const params = new URLSearchParams();
  if (query.state) params.set("state", query.state);
  if (query.lane) params.set("lane", query.lane);
  if (query.session_id) params.set("session_id", query.session_id);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return requestJson<JobInfo[]>(`/api/jobs${suffix}`);
}

export function getJob(id: string): Promise<JobInfo> {
  return requestJson<JobInfo>(`/api/jobs/${encodeURIComponent(id)}`);
}

export function cancelJob(id: string): Promise<JobInfo> {
  return requestJson<JobInfo>(`/api/jobs/${encodeURIComponent(id)}/cancel`, jsonBody({}));
}
