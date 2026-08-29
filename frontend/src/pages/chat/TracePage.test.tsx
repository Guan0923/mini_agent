import { App as AntApp } from "antd";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getTurnTrace } from "../../api";
import { TURN_PROTOCOL_VERSION } from "../../app/runtime/runtimeNodeNormalization";
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
    version: TURN_PROTOCOL_VERSION,
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
    project_cwd: "",
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

function outerTurnPanel(turnId: string): HTMLElement {
  const panel = screen.getByTitle(turnId).closest(".ant-collapse-item");
  if (!(panel instanceof HTMLElement)) throw new Error(`Turn panel ${turnId} is missing.`);
  return panel;
}

function clickTurnHeader(turnId: string): void {
  const header = outerTurnPanel(turnId).querySelector(":scope > .ant-collapse-header");
  if (!(header instanceof HTMLElement)) throw new Error(`Turn header ${turnId} is missing.`);
  fireEvent.click(header);
}

describe("TracePage", () => {
  it("renders every Turn oldest first and loads only the latest Turn initially", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const sameTimestampA = turn("turn-a", "2026-08-28T00:00:00Z");
    const latest = turn("turn-b", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      [older, sameTimestampA, latest].find((candidate) => candidate.id === turnId)!,
      dataIdx,
    ));

    const { container } = render(<AntApp><TracePage turns={[latest, older, sameTimestampA]} /></AntApp>);

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-b", 1, expect.any(AbortSignal), undefined,
    ));
    expect([...container.querySelectorAll(".trace-turn-id")].map((element) => element.textContent))
      .toEqual(["turn-old", "turn-a", "turn-b"]);
    expect(screen.queryByRole("combobox", { name: "选择 Turn" })).not.toBeInTheDocument();
    expect(outerTurnPanel("turn-old")).not.toHaveClass("ant-collapse-item-active");
    expect(outerTurnPanel("turn-a")).not.toHaveClass("ant-collapse-item-active");
    expect(outerTurnPanel("turn-b")).toHaveClass("ant-collapse-item-active");
    expect(getTurnTrace).toHaveBeenCalledTimes(1);
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

  it("switches each Turn data version independently without toggling its panel", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      turnId === older.id ? older : latest,
      dataIdx,
    ));
    render(<AntApp><TracePage turns={[latest, older]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 1, expect.any(AbortSignal), undefined,
    ));

    fireEvent.click(screen.getByRole("button", { name: "turn-old 上一个 data 版本" }));
    expect(outerTurnPanel("turn-old")).not.toHaveClass("ant-collapse-item-active");
    expect(vi.mocked(getTurnTrace).mock.calls.some(([turnId]) => turnId === "turn-old")).toBe(false);

    clickTurnHeader("turn-old");
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-old", 0, expect.any(AbortSignal), undefined,
    ));
    expect(outerTurnPanel("turn-old")).toHaveClass("ant-collapse-item-active");
    expect(outerTurnPanel("turn-new")).toHaveClass("ant-collapse-item-active");

    fireEvent.click(screen.getByRole("button", { name: "turn-new 上一个 data 版本" }));
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 0, expect.any(AbortSignal), undefined,
    ));
    expect(outerTurnPanel("turn-new")).toHaveClass("ant-collapse-item-active");
    expect(screen.getAllByText("1/2")).toHaveLength(2);
  });

  it("aborts a Turn request when collapsed and reloads its full baseline when reopened", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    let olderCalls = 0;
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => {
      if (turnId === latest.id) return response(latest, dataIdx);
      olderCalls += 1;
      if (olderCalls === 1) return new Promise<TurnTraceResponse>(() => undefined);
      return response(older, dataIdx);
    });
    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-new", 1, expect.any(AbortSignal), undefined,
    ));

    clickTurnHeader("turn-old");
    await waitFor(() => expect(olderCalls).toBe(1));
    const firstOlderCall = vi.mocked(getTurnTrace).mock.calls.find(([turnId]) => turnId === older.id);
    expect(firstOlderCall).toBeDefined();

    clickTurnHeader("turn-old");
    await waitFor(() => expect(firstOlderCall?.[2]?.aborted).toBe(true));
    expect(outerTurnPanel("turn-old")).not.toHaveClass("ant-collapse-item-active");

    clickTurnHeader("turn-old");
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-old", 1, expect.any(AbortSignal), undefined,
    ));
    expect(olderCalls).toBe(2);
    expect(screen.getAllByText("System")).toHaveLength(2);
  });

  it("stops scheduled polling when a running Turn is collapsed", async () => {
    vi.useFakeTimers();
    const running = turn("turn-running", "2026-08-28T00:00:00Z", "running");
    vi.mocked(getTurnTrace).mockResolvedValue(response(running, 1));
    render(<AntApp><TracePage turns={[running]} /></AntApp>);
    await act(async () => Promise.resolve());
    await act(async () => Promise.resolve());
    expect(getTurnTrace).toHaveBeenCalledTimes(1);

    clickTurnHeader("turn-running");
    await act(async () => Promise.resolve());
    await act(async () => {
      vi.advanceTimersByTime(6_000);
      await Promise.resolve();
    });

    expect(outerTurnPanel("turn-running")).not.toHaveClass("ant-collapse-item-active");
    expect(getTurnTrace).toHaveBeenCalledTimes(1);
  });

  it("isolates a failed Turn while another expanded Turn loads normally", async () => {
    const older = turn("turn-old", "2026-08-27T00:00:00Z");
    const latest = turn("turn-new", "2026-08-28T00:00:00Z");
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => {
      if (turnId === latest.id) throw new Error("latest unavailable");
      return response(older, dataIdx);
    });
    render(<AntApp><TracePage turns={[older, latest]} /></AntApp>);

    expect(await screen.findByText("Trace 加载失败：latest unavailable")).toBeInTheDocument();
    clickTurnHeader("turn-old");
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "turn-old", 1, expect.any(AbortSignal), undefined,
    ));
    expect(screen.getByText("System")).toBeInTheDocument();
    expect(screen.getByText("Trace 加载失败：latest unavailable")).toBeInTheDocument();
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

  it("resets to only the latest Turn when the keyed Trace page switches Threads", async () => {
    const firstOlder = turn("first-old", "2026-08-27T00:00:00Z");
    const firstLatest = turn("first-new", "2026-08-28T00:00:00Z");
    const secondOlder = { ...turn("second-old", "2026-08-27T00:00:00Z"), thread_id: "thread-b" };
    const secondLatest = { ...turn("second-new", "2026-08-28T00:00:00Z"), thread_id: "thread-b" };
    vi.mocked(getTurnTrace).mockImplementation(async (turnId, dataIdx) => response(
      [firstOlder, firstLatest, secondOlder, secondLatest].find((candidate) => candidate.id === turnId)!,
      dataIdx,
    ));
    const { rerender } = render(
      <AntApp><TracePage key="thread-a" turns={[firstOlder, firstLatest]} /></AntApp>,
    );
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "first-new", 1, expect.any(AbortSignal), undefined,
    ));
    clickTurnHeader("first-old");
    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "first-old", 1, expect.any(AbortSignal), undefined,
    ));

    rerender(<AntApp><TracePage key="thread-b" turns={[secondLatest, secondOlder]} /></AntApp>);

    await waitFor(() => expect(getTurnTrace).toHaveBeenCalledWith(
      "second-new", 1, expect.any(AbortSignal), undefined,
    ));
    expect(outerTurnPanel("second-old")).not.toHaveClass("ant-collapse-item-active");
    expect(outerTurnPanel("second-new")).toHaveClass("ant-collapse-item-active");
    expect(vi.mocked(getTurnTrace).mock.calls.some(([turnId]) => turnId === "second-old")).toBe(false);
  });
});
