import { afterEach, describe, expect, it, vi } from "vitest";

import { SIDEBAR_REFRESH_INTERVAL_MS, subscribeVisibleSidebarRefresh } from "./sidebarRefresh";

describe("visible Sidebar summary refresh", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("polls every five seconds only while visible and refreshes immediately on return", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "visible";
    vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    const refresh = vi.fn().mockResolvedValue(undefined);
    const unsubscribe = subscribeVisibleSidebarRefresh(refresh);

    await vi.advanceTimersByTimeAsync(SIDEBAR_REFRESH_INTERVAL_MS - 1);
    expect(refresh).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(refresh).toHaveBeenCalledTimes(1);

    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(SIDEBAR_REFRESH_INTERVAL_MS * 2);
    expect(refresh).toHaveBeenCalledTimes(1);

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();
    expect(refresh).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(SIDEBAR_REFRESH_INTERVAL_MS);
    expect(refresh).toHaveBeenCalledTimes(3);

    unsubscribe();
    await vi.advanceTimersByTimeAsync(SIDEBAR_REFRESH_INTERVAL_MS);
    expect(refresh).toHaveBeenCalledTimes(3);
  });
});
