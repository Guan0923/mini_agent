import { App as AntApp } from "antd";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getTurnTrace } from "../../api";
import type { RuntimeStateNode, TurnTraceResponse } from "../../types";
import TracePage from "./TracePage";

vi.mock("../../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api")>(),
  getTurnTrace: vi.fn(),
}));

function turn(id: string, timestamp: string, status: RuntimeStateNode["status"] = "success"): RuntimeStateNode {
  return {
    thread_id: "thread-a",
    parent_thread_id: "",
    session_id: "session-a",
    parent_session_id: "",
    id,
    parent_id: "",
    version: "0.0.1",
    firstKeptItemSize: 8,
    compactionId: id,
    user: "",
    provider_name: "local",
    model: {
      reasoning_effort: "medium",
      current_model: "fake",
      context_length: 4096,
      output_length: 512,
      thinking: "enable",
      temperature: 0,
    },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1, reasoning_tokens: 0, total_tokens: 2 },
    cwd: "C:\\workspace",
    timestamp,
    status,
    current_data_idx: 1,
    data: [0, 1].map((version) => [
      { role: "user", content: [{ type: "text", text: `question-${version}`, status: "success" }] },
      { role: "assistant", content: [
        { type: "reasoning", text: `reason-${version}`, status: "success" },
        { type: "text", text: `answer-${version}`, status: "success" },
      ] },
    ]),
  };
}

function response(value: RuntimeStateNode, dataIdx: number): TurnTraceResponse {
  return {
    turn: value,
    data_idx: dataIdx,
    requests: [{
      schema_version: 1,
      turn_id: value.id,
      thread_id: value.thread_id,
      data_idx: dataIdx,
      exchange_id: "exchange-1",
      sequence: 1,
      timestamp: value.timestamp,
      provider: "responses",
      provider_name: "local",
      model: "fake",
      operation: "decision",
      output_mode: "tools",
      stream: true,
      base_system_prompt: "base system",
      effective_system_prompt: "effective system",
      messages: [{ role: "user", content: "audit user" }],
      user_preferences: "concise",
      skills: [{ name: "demo", instructions: "skill instructions" }],
      tools: [{
        name: "mcp_demo_search",
        description: "Search",
        parameters: { type: "object" },
        origin: { kind: "mcp", server: "demo", tool: "search" },
      }],
      request_parameters: { temperature: 0 },
    }],
  };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("TracePage", () => {
  it("defaults to the latest Turn and renders semantic labels", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      turnId === latest.id ? latest : older,
      dataIdx,
    ));

    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith("turn-new", 1, expect.any(AbortSignal)));
    expect(screen.getAllByText("System").length).toBeGreaterThan(0);
    expect(screen.getByText("Preference")).toBeInTheDocument();
    expect(screen.getByText("Skill")).toBeInTheDocument();
    expect(screen.getByText("MCP")).toBeInTheDocument();
    expect(screen.getAllByText("User Message").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Assistant Reasoning").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Assistant Response").length).toBeGreaterThan(0);
    expect(screen.getByText("Preference").closest(".ant-tag")).toHaveClass("ant-tag-magenta");
    expect(screen.getByText("Skill").closest(".ant-tag")).toHaveClass("ant-tag-cyan");
    expect(screen.getByText("MCP").closest(".ant-tag")).toHaveClass("ant-tag-orange");
    expect(screen.getAllByText("User Message")[0].closest(".ant-tag")).toHaveClass("ant-tag-green");
  });

  it("switches data versions locally without changing the Turn", async () => {
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (_turnId, dataIdx) => response(latest, dataIdx));
    render(<AntApp><TracePage turns={[latest]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith("turn-new", 1, expect.any(AbortSignal)));

    fireEvent.click(screen.getByRole("button", { name: "上一个 data 版本" }));

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith("turn-new", 0, expect.any(AbortSignal)));
    expect(screen.getByText("data 1/2")).toBeInTheDocument();
  });

  it("selects another Turn without changing the Thread", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      turnId === latest.id ? latest : older,
      dataIdx,
    ));
    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith("turn-new", 1, expect.any(AbortSignal)));

    fireEvent.mouseDown(screen.getByRole("combobox", { name: "选择 Turn" }));
    fireEvent.click(await screen.findByText("turn-old · success"));

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith("turn-old", 1, expect.any(AbortSignal)));
  });

  it("polls a running Turn and stops after it reaches a terminal status", async () => {
    vi.useFakeTimers();
    const running = turn("turn-running", "2026-08-28T00:00:00Z", "running");
    const finished = { ...running, status: "success" as const };
    vi.mocked(getTurnTrace)
      .mockResolvedValueOnce(response(running, 1))
      .mockResolvedValue(response(finished, 1));
    render(<AntApp><TracePage turns={[running]} /></AntApp>);
    await act(async () => Promise.resolve());
    expect(getTurnTrace).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
    });
    await act(async () => Promise.resolve());
    const terminalCallCount = vi.mocked(getTurnTrace).mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(6_000);
      await Promise.resolve();
    });
    expect(getTurnTrace).toHaveBeenCalledTimes(terminalCallCount);
  });
});
