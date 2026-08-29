import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, requestVoid } from "./request";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("requestVoid", () => {
  it("accepts a successful 204 response without parsing JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(requestVoid("/api/example", { method: "DELETE" })).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("preserves structured errors from unsuccessful responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: "删除失败", code: "delete_failed" }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    )));

    await expect(requestVoid("/api/example", { method: "DELETE" })).rejects.toEqual(
      new ApiError(409, "删除失败", "delete_failed"),
    );
  });
});
