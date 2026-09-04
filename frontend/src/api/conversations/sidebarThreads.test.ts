import { afterEach, describe, expect, it, vi } from "vitest";
import { updateSidebarThreadOrder } from "./sidebarThreads";

afterEach(() => vi.unstubAllGlobals());

describe("SidebarThread order API", () => {
  it("sends a complete manual order for one group", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ordered_thread_ids: ["thread_b", "thread_a"] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await updateSidebarThreadOrder("project_1", { orderedThreadIds: ["thread_b", "thread_a"] });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      project_id: "project_1",
      ordered_thread_ids: ["thread_b", "thread_a"],
    });
  });

  it("sends a one-time recent activity sort for the unassigned group", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ordered_thread_ids: ["thread_a"] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await updateSidebarThreadOrder(null, { sortBy: "recent_activity" });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      project_id: null,
      sort_by: "recent_activity",
    });
  });
});
