/** Shared browser request helpers used by all API domains. */

import { apiUrl } from "./base";

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string) {
    super(message);
    this.name = "ApiError";
  }
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

async function request(url: string, init: RequestInit): Promise<Response> {
  const res = await fetch(apiUrl(url), init);
  if (!res.ok) {
    const details = await errorDetailsFrom(res);
    throw new ApiError(res.status, details.message, details.code);
  }
  return res;
}

export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await request(url, init);
  return res.json() as Promise<T>;
}

export async function requestVoid(url: string, init: RequestInit = {}): Promise<void> {
  await request(url, init);
}

export function jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
