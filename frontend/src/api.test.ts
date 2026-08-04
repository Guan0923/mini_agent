import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser, login, streamChat } from "./api";
import type { StreamMessage } from "./types";

afterEach(() => vi.restoreAllMocks());

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
});
