import { beforeEach, describe, expect, it } from "vitest";
import { countUnreadArchived, loadArchiveReadState, loadConversations, markArchivedAsRead } from "./App";
import { BROWSER_STATE_VERSION, BROWSER_STATE_VERSION_KEY, resetLegacyBrowserState } from "./app/storage";
import type { Conversation } from "./types";

beforeEach(() => localStorage.clear());

describe("conversation recovery", () => {
  it("does not restore a stale running animation from localStorage", () => {
    localStorage.setItem(
      "mini-agent-conversations",
      JSON.stringify([
        {
          id: "old-run",
          title: "旧任务",
          messages: [
            { id: "assistant-1", role: "assistant", content: "", events: [], running: true },
          ],
        },
      ]),
    );

    const conversations = loadConversations("mini-agent-conversations");
    expect(conversations[0]?.messages[0]).toMatchObject({
      running: false,
      status: "上次运行已中断",
    });
  });

  it("persists archive reads by conversation archive timestamp", () => {
    const archived = [{ id: "archived-1", title: "旧对话", messages: [], archivedAt: "2026-08-05T00:00:00Z" }] as Conversation[];
    const initial = {};

    expect(countUnreadArchived(archived, initial)).toBe(1);
    const read = markArchivedAsRead(initial, archived);
    expect(countUnreadArchived(archived, read)).toBe(0);
    expect(markArchivedAsRead(read, archived)).toBe(read);

    localStorage.setItem("mini-agent-archive-read:user-1", JSON.stringify(read));
    expect(loadArchiveReadState("user-1")).toEqual(read);
    expect(countUnreadArchived([...archived, { ...archived[0], id: "archived-2" }], read)).toBe(1);
  });

  it("clears pre-RuntimeState browser history once", () => {
    localStorage.setItem("mini-agent-conversations:user-1", "old history");
    localStorage.setItem("mini-agent-archive-read:user-1", "old archive state");
    localStorage.setItem("mini-agent-session-modes", "old modes");

    resetLegacyBrowserState();

    expect(localStorage.getItem("mini-agent-conversations:user-1")).toBeNull();
    expect(localStorage.getItem("mini-agent-archive-read:user-1")).toBeNull();
    expect(localStorage.getItem("mini-agent-session-modes")).toBeNull();
    expect(localStorage.getItem(BROWSER_STATE_VERSION_KEY)).toBe(BROWSER_STATE_VERSION);

    localStorage.setItem("mini-agent-conversations:user-1", "new history");
    resetLegacyBrowserState();
    expect(localStorage.getItem("mini-agent-conversations:user-1")).toBe("new history");
  });
});
