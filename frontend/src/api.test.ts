import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser, login, streamChat, streamResume } from "./api";
import type { StreamMessage } from "./types";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

function responseWithChunks(...chunks: string[]) {
  const encoder = new TextEncoder();
  let index = 0;
  const reader = {
    read: vi.fn(async () => {
      if (index >= chunks.length) return { done: true, value: undefined };
      return { done: false, value: encoder.encode(chunks[index++]) };
    }),
    releaseLock: vi.fn(),
  };
  return { ok: true, body: { getReader: () => reader } } as unknown as Response;
}

describe("streamChat", () => {
  it("returns completed after a terminal done frame", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithChunks('data: {"type":"done","status":"completed"}\n\n')));
    const messages: StreamMessage[] = [];
    await expect(streamChat("task", (message) => messages.push(message), new AbortController().signal)).resolves.toBe(
      "completed",
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].type).toBe("done");
  });

  it("reports an early EOF instead of silently leaving the caller running", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithChunks("data: {\"type\":\"event\"}\n\n")));
    await expect(streamChat("task", () => undefined, new AbortController().signal)).rejects.toThrow(
      "SSE stream unexpectedly ended",
    );
  });

  it("turns an AbortError into an aborted result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));
    const controller = new AbortController();
    controller.abort();
    await expect(streamChat("task", () => undefined, controller.signal)).resolves.toBe("aborted");
  });

  it("snapshots permission and reasoning effort in the chat request body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithChunks('data: {"type":"done","status":"completed"}\n\n')));

    await streamChat("task", () => undefined, new AbortController().signal, {
      sessionId: "session-1",
      mode: "plan",
      permissionMode: "full_access",
      reasoningEffort: "xhigh",
    });

    const fetchMock = vi.mocked(fetch);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/chat");
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({
      prompt: "task",
      session_id: "session-1",
      mode: "plan",
      permission_mode: "full_access",
      reasoning_effort: "xhigh",
      interactive: true,
    });
  });

  it("includes reasoning effort when resuming a session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithChunks('data: {"type":"done","status":"completed"}\n\n')));

    await streamResume("session-2", () => undefined, new AbortController().signal, "approval_for_me", "high");

    const fetchMock = vi.mocked(fetch);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/sessions/session-2/resume");
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toEqual({
      permission_mode: "approval_for_me",
      reasoning_effort: "high",
    });
  });

  it("streams through the configured API subdomain", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(responseWithChunks('data: {"type":"done"}\n\n')));

    await streamChat("task", () => undefined, new AbortController().signal);

    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe("https://api.example.com/api/chat");
    expect((vi.mocked(fetch).mock.calls[0]?.[1] as RequestInit).credentials).toBe("include");
  });
});

describe("web auth API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("treats an unauthenticated me request as an anonymous user", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "请先登录。" }), { status: 401 })));
    await expect(getCurrentUser()).resolves.toBeNull();
    const fetchMock = vi.mocked(fetch);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/auth/me");
    expect(fetchMock.mock.calls[0]?.[1]).toEqual({ credentials: "include" });
  });

  it("sends login JSON with browser credentials enabled", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ user: { id: "u1", email: "a@example.com", legacy_owner: false } }), { status: 200 })));
    await expect(login("a@example.com", "a".repeat(12))).resolves.toMatchObject({ id: "u1" });
    const fetchMock = vi.mocked(fetch);
    const options = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(options.credentials).toBe("include");
    expect(options.method).toBe("POST");
    expect(options.body).toBe(JSON.stringify({ email: "a@example.com", password: "a".repeat(12) }));
  });

  it("targets a configured API subdomain while retaining browser credentials", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com/");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ user: { id: "u1", email: "a@example.com", legacy_owner: false } }), { status: 200 })));

    await login("a@example.com", "a".repeat(12));

    const fetchMock = vi.mocked(fetch);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("https://api.example.com/api/auth/login");
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).credentials).toBe("include");
  });
});
