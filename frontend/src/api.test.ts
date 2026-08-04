import { beforeEach, describe, expect, it, vi } from "vitest";
import { getCurrentUser, login } from "./api";

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
