import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App, { countUnreadArchived, loadArchiveReadState, markArchivedAsRead } from "./App";
import { BROWSER_STATE_VERSION, BROWSER_STATE_VERSION_KEY, resetLegacyBrowserState } from "./app/storage";
import type { Conversation } from "./types";

beforeEach(() => localStorage.clear());

vi.mock("./app/AgentApp", () => ({
  default: () => <main data-testid="chat-page">Chat</main>,
}));

describe("local-only routes", () => {
  it("renders Chat directly at root and redirects removed account pages without auth requests", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    window.history.replaceState({}, "", "/login");

    render(<App />);

    expect(screen.getByTestId("chat-page")).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(screen.queryByText(/登录|注册|退出|云同步/)).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([input]) => String(input).includes("/api/auth/"))).toBe(false);
    fetchSpy.mockRestore();
  });
});

describe("conversation recovery", () => {
  it("persists archive reads by conversation archive timestamp", () => {
    const archived = [{ id: "archived-1", title: "旧对话", messages: [], archivedAt: "2026-08-05T00:00:00Z" }] as Conversation[];
    const initial = {};

    expect(countUnreadArchived(archived, initial)).toBe(1);
    const read = markArchivedAsRead(initial, archived);
    expect(countUnreadArchived(archived, read)).toBe(0);
    expect(markArchivedAsRead(read, archived)).toBe(read);

    localStorage.setItem("mini-agent-archive-read", JSON.stringify(read));
    expect(loadArchiveReadState()).toEqual(read);
    expect(countUnreadArchived([...archived, { ...archived[0], id: "archived-2" }], read)).toBe(1);
  });

  it("clears conversation data while preserving UI-only preferences", () => {
    localStorage.setItem("mini-agent-conversations:user-1", "old history");
    localStorage.setItem("mini-agent-archive-read:user-1", "old archive state");
    localStorage.setItem("mini-agent-session-modes", "old modes");

    resetLegacyBrowserState();

    expect(localStorage.getItem("mini-agent-conversations:user-1")).toBeNull();
    expect(localStorage.getItem("mini-agent-archive-read:user-1")).toBe("old archive state");
    expect(localStorage.getItem("mini-agent-session-modes")).toBe("old modes");
    expect(localStorage.getItem(BROWSER_STATE_VERSION_KEY)).toBe(BROWSER_STATE_VERSION);

  });
});
