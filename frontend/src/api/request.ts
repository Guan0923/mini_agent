/** Shared browser request helpers used by all API domains. */

import { apiUrl } from "./base";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
}

let unauthorizedHandler: (() => void) | null = null;

/** Register the app-level response to an expired/revoked browser session. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

export interface ApiErrorDetails {
  message: string;
  code?: string;
}

export async function errorDetailsFrom(res: Response): Promise<ApiErrorDetails> {
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") {
      return {
        message: body.detail,
        code: typeof body.code === "string" ? body.code : undefined,
      };
    }
  } catch {
    /* fall through */
  }
  return { message: `HTTP ${res.status}` };
}

export async function errorFrom(res: Response): Promise<string> {
  return (await errorDetailsFrom(res)).message;
}

export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(apiUrl(url), { credentials: "include", ...init });
  if (!res.ok) {
    if (res.status === 401) notifyUnauthorized();
    const details = await errorDetailsFrom(res);
    throw new ApiError(res.status, details.message, details.code);
  }
  return res.json() as Promise<T>;
}

export function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
