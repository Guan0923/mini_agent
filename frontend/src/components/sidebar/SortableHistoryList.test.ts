import { describe, expect, it } from "vitest";
import { reorderHistoryIds } from "./SortableHistoryList";

describe("reorderHistoryIds", () => {
  it("moves one row without changing ids outside the group", () => {
    expect(reorderHistoryIds(["a", "b", "c"], "a", "c")).toEqual(["b", "c", "a"]);
    expect(reorderHistoryIds(["a", "b", "c"], "missing", "c")).toEqual(["a", "b", "c"]);
  });
});
