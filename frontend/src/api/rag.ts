import { apiUrl } from "./base";
import { ApiError, errorFrom, jsonBody, notifyUnauthorized, requestJson } from "./request";

export interface RagTreeDocument {
  document_id: string;
  user_id: string;
  section_id: string;
  filename: string;
  relative_path: string;
  size_bytes: number;
  sha256: string;
  status: "queued" | "indexing" | "ready" | "not_imported" | "stale" | "failed";
  source: "project" | "upload" | string;
  created_at: number;
  error: string | null;
  ingestion_status: string | null;
  ingestion_error: string | null;
}

export interface RagTreeSection {
  section: {
    section_id: string;
    user_id: string;
    section_type: "project" | "session";
    project_id: string | null;
    session_id: string | null;
    display_name: string;
    created_at: number;
  };
  documents: RagTreeDocument[];
}

export interface RagDocumentMutation {
  document: RagTreeDocument;
  ingestion: {
    document_id: string;
    embedding_profile_id: string;
    status: RagTreeDocument["status"];
    created_at: number;
    updated_at: number;
    error?: string | null;
  };
  job_id: string | null;
}

export interface RagUploadResult extends RagDocumentMutation {
  duplicate: boolean;
  section: RagTreeSection["section"];
}

export interface RagDeleteResult {
  deleted: string;
  warning: string | null;
}

export async function getRagTree(): Promise<RagTreeSection[]> {
  return requestJson<RagTreeSection[]>("/api/rag/tree");
}

export async function uploadRagDocument(sectionId: string, file: File): Promise<RagUploadResult> {
  const form = new FormData();
  form.append("section_id", sectionId);
  form.append("file", file, file.name);
  const response = await fetch(apiUrl("/api/rag/documents/upload"), {
    method: "POST",
    body: form,
    credentials: "include",
  });
  if (!response.ok) {
    if (response.status === 401) notifyUnauthorized();
    throw new ApiError(response.status, await errorFrom(response));
  }
  return response.json() as Promise<RagUploadResult>;
}

export function reindexRagDocument(documentId: string): Promise<RagDocumentMutation> {
  return requestJson<RagDocumentMutation>(
    `/api/rag/documents/${encodeURIComponent(documentId)}/reindex`,
    jsonBody({}),
  );
}

export function deleteRagDocument(documentId: string): Promise<RagDeleteResult> {
  return requestJson<RagDeleteResult>(`/api/rag/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
  });
}
