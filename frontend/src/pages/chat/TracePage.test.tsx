import { App as AntApp } from "antd";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getTurnTrace } from "../../api";
import type { RuntimeStateNode, TurnTraceItem, TurnTraceResponse } from "../../types";
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

function traceItem(
  sequence: number,
  messageIdx: number,
  itemIdx: number,
  role: "user" | "assistant",
  item: TurnTraceItem["item"],
): TurnTraceItem {
  return {
    sequence,
    message_idx: messageIdx,
    item_idx: itemIdx,
    role,
    item,
    completed_at: `2026-08-28T00:00:0${sequence}Z`,
  };
}

function response(
  value: RuntimeStateNode,
  dataIdx: number,
  options: { context?: boolean; items?: TurnTraceItem[] } = {},
): TurnTraceResponse {
  const items = options.items ?? [
    traceItem(1, 0, 0, "user", { type: "text", text: `question-${dataIdx}`, status: "success" }),
    traceItem(2, 1, 0, "assistant", { type: "reasoning", text: `reason-${dataIdx}`, status: "success" }),
    traceItem(3, 1, 1, "assistant", { type: "text", text: `answer-${dataIdx}`, status: "success" }),
  ];
  return {
    turn: value,
    data_idx: dataIdx,
    context: options.context === false ? null : {
      system_message: "base system\n\n## User Agent Preferences\nconcise",
      initialized_at: value.timestamp,
      active_skills: [{ name: "demo", instructions: "skill instructions" }],
      tools: [{
        name: "mcp_demo_search",
        description: "Search",
        parameters: { type: "object" },
        origin: { kind: "mcp", server: "demo", tool: "search" },
      }],
    },
    items,
    last_sequence: Math.max(0, ...items.map((item) => item.sequence)),
  };
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("TracePage", () => {
  it("defaults to the latest Turn and renders one context plus traced Items", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      turnId === latest.id ? latest : older,
      dataIdx,
    ));

    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 1, expect.any(AbortSignal), undefined,
    ));
    expect(screen.getAllByText("System")).toHaveLength(1);
    expect(screen.getAllByText("Skill")).toHaveLength(1);
    expect(screen.getAllByText("MCP")).toHaveLength(1);
    expect(screen.getAllByText("User Message")).toHaveLength(1);
    expect(screen.getAllByText("Assistant Reasoning")).toHaveLength(1);
    expect(screen.getAllByText("Assistant Response")).toHaveLength(1);
    expect(screen.getByText("Skill").closest(".ant-tag")).toHaveClass("ant-tag-cyan");
    expect(screen.getByText("MCP").closest(".ant-tag")).toHaveClass("ant-tag-orange");
    expect(screen.getByText("User Message").closest(".ant-tag")).toHaveClass("ant-tag-green");
  });

  it("switches data versions locally without changing the Turn", async () => {
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (_turnId, dataIdx) => response(latest, dataIdx));
    render(<AntApp><TracePage turns={[latest]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 1, expect.any(AbortSignal), undefined,
    ));

    fireEvent.click(screen.getByRole("button", { name: "上一个 data 版本" }));

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 0, expect.any(AbortSignal), undefined,
    ));
    expect(screen.getByText("data 1/2")).toBeInTheDocument();
  });

  it("selects another Turn and aborts the obsolete request", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      turnId === latest.id ? latest : older,
      dataIdx,
    ));
    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalled());
    const firstSignal = vi.mocked(getTurnTrace).mock.calls[0][2];

    fireEvent.mouseDown(screen.getByRole("combobox", { name: "选择 Turn" }));
    fireEvent.click(await screen.findByText("turn-old · success"));

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-old", 1, expect.any(AbortSignal), undefined,
    ));
    expect(firstSignal?.aborted).toBe(true);
  });

  it("marks outer and inner Collapse titles for single-line truncation", async () => {
    const longTurnId = `turn-${"x".repeat(180)}`;
    const longPreview = `system-${"very-long-trace-content-".repeat(40)}`;
    const latest = turn(longTurnId, "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (_turnId, dataIdx) => {
      const value = response(latest, dataIdx);
      value.context!.system_message = longPreview;
      return value;
    });
    const { container } = render(<AntApp><TracePage turns={[latest]} /></AntApp>);

    await waitFor(() => expect(screen.getByTitle(longPreview)).toBeInTheDocument());
    const semanticTitles = container.querySelectorAll(".trace-collapse-title");
    expect(semanticTitles.length).toBeGreaterThan(1);
    expect(screen.getByTitle(longTurnId)).toHaveClass("trace-turn-id");
    expect(screen.getByTitle(longPreview)).toHaveClass("trace-preview");
  });

  it("keeps the baseline and merges only incremental Items until the Turn finishes", async () => {
    vi.useFakeTimers();
    const running = turn("turn-running", "2026-08-28T00:00:00Z", "running");
    const finished = { ...running, status: "success" as const };
    const initial = response(running, 1, {
      items: [traceItem(1, 0, 0, "user", { type: "text", text: "question-1", status: "success" })],
    });
    const incremental = response(finished, 1, {
      context: false,
      items: [traceItem(2, 1, 0, "assistant", { type: "text", text: "incremental answer", status: "success" })],
    });
    vi.mocked(getTurnTrace).mockResolvedValueOnce(initial).mockResolvedValue(incremental);
    render(<AntApp><TracePage turns={[running]} /></AntApp>);
    await act(async () => Promise.resolve());
    expect(screen.getByText("System")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
    });
    await act(async () => Promise.resolve());
    expect(getTurnTrace).toHaveBeenLastCalledWith(
      "turn-running", 1, expect.any(AbortSignal), 1,
    );
    expect(screen.getByText("System")).toBeInTheDocument();
    expect(screen.getByTitle("question-1")).toBeInTheDocument();
    expect(screen.getByTitle("incremental answer")).toBeInTheDocument();
    const terminalCallCount = vi.mocked(getTurnTrace).mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(6_000);
      await Promise.resolve();
    });
    expect(getTurnTrace).toHaveBeenCalledTimes(terminalCallCount);
  });

  it("retries a full baseline while the first decision has not initialized context", async () => {
    vi.useFakeTimers();
    const running = turn("turn-running", "2026-08-28T00:00:00Z", "running");
    vi.mocked(getTurnTrace)
      .mockResolvedValueOnce({ turn: running, data_idx: 1, context: null, items: [], last_sequence: 0 })
      .mockResolvedValue(response({ ...running, status: "success" }, 1));
    render(<AntApp><TracePage turns={[running]} /></AntApp>);
    await act(async () => Promise.resolve());

    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
    });
    await act(async () => Promise.resolve());
    expect(getTurnTrace).toHaveBeenLastCalledWith(
      "turn-running", 1, expect.any(AbortSignal), undefined,
    );
    expect(screen.getByText("System")).toBeInTheDocument();
  });
});
