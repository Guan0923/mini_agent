import type { FileReference, FileSource, SessionFileInfo } from "../types";
import { apiUrl } from "./base";
import { ApiError, errorFrom, notifyUnauthorized } from "./request";

/** Upload a batch of files; resolves to the stored file metadata. */
export async function uploadSessionFiles(
  sessionId: string,
  files: File[],
  onProgress?: (percent: number) => void,
): Promise<SessionFileInfo[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  const response = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files`), {
    method: "POST",
    body: form,
    credentials: "include",
  });
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, await errorFrom(response));
  }
  return response.json() as Promise<SessionFileInfo[]>;
}

/** Search project + upload roots for one session. */
export async function searchSessionFiles(
  sessionId: string,
  q: string,
  limit = 20,
): Promise<SessionFileInfo[]> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const response = await fetch(
    apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files?${params.toString()}`),
    { credentials: "include" },
  );
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, await errorFrom(response));
  }
  return response.json() as Promise<SessionFileInfo[]>;
}

/** Build the authenticated content URL for preview/download. */
export function sessionFileContentUrl(
  sessionId: string,
  source: FileSource,
  path: string,
  download = false,
): string {
  const params = new URLSearchParams({ source, path, download: download ? "true" : "" });
  return apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files/content?${params.toString()}`);
}

/** Delete one session-uploaded file (project files are never deletable). */
export async function deleteSessionFile(
  sessionId: string,
  source: FileSource,
  path: string,
): Promise<void> {
  const params = new URLSearchParams({ source, path });
  const response = await fetch(
    apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/files?${params.toString()}`),
    { method: "DELETE", credentials: "include" },
  );
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, await errorFrom(response));
  }
}

/** Probe whether a referenced file is still available for display. */
export async function fileReferenceAvailable(reference: FileReference, sessionId: string): Promise<boolean> {
  const url = sessionFileContentUrl(sessionId, reference.source, reference.path);
  try {
    const response = await fetch(url, { method: "HEAD", credentials: "include" });
    return response.ok;
  } catch {
    return false;
  }
}
