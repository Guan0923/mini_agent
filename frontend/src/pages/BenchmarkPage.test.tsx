import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BenchmarkPage from "./BenchmarkPage";
import type { TaskInfo } from "../types";

const mocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  runBenchmark: vi.fn(),
  runAllBenchmark: vi.fn(),
}));

vi.mock("../api", () => mocks);

const task = (name: string): TaskInfo => ({
  name,
  capability: "software_engineering",
  description: "修复一个需要较长说明的适配任务",
  difficulty: "中等",
  prompt: "请修复这个适配任务并说明原因。",
  budgets: { max_model_turns: 8, max_tool_calls: 32, max_replans: 2, max_retries: 1 },
  tags: ["适配"],
  source: {
    benchmark: "SWE-bench",
    task_id: "owner/repository#123",
    url: "https://example.com/owner/repository/issues/123",
    source_revision: "abc123",
    license: "MIT",
    adaptation_notes: "保留原始任务约束和评测说明。",
  },
  planner_modes: ["llm"],
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listTasks.mockResolvedValue([task("task-one"), task("task-two")]);
  mocks.runBenchmark.mockResolvedValue({
    task_name: "task-one",
    score: 0.9,
    passed: true,
    final_answer: "已完成",
    trace: [{ kind: "tool_call", timestamp: "2026-01-01T00:00:00Z", message: "读取文件", data: { path: "safe.txt" } }],
  });
  mocks.runAllBenchmark.mockResolvedValue([
    { task_name: "task-one", score: 0.9, passed: true },
    { task_name: "task-two", score: 0.5, passed: false },
  ]);
});

describe("BenchmarkPage layout and runs", () => {
  afterEach(() => cleanup());

  it("renders wide task cards through the shared two-column grid", async () => {
    const { container } = render(<BenchmarkPage />);

    expect(await screen.findByText("task-one")).toBeInTheDocument();
    expect(container.querySelector(".task-grid")).toBeInTheDocument();
    expect(container.querySelectorAll(".task-card")).toHaveLength(2);
    expect(container.querySelectorAll(".ant-col-lg-12")).toHaveLength(2);
  });

  it("loads, runs one task, and runs all tasks", async () => {
    const user = userEvent.setup();
    render(<BenchmarkPage />);
    await screen.findByText("task-one");

    const runButtons = screen.getAllByRole("button").filter((button) => button.textContent?.trim() === "运行");
    await user.click(runButtons[0]);
    await waitFor(() => expect(mocks.runBenchmark).toHaveBeenCalledWith("task-one", "llm"));
    expect(await screen.findByText("状态：通过")).toBeInTheDocument();
    expect(document.querySelector(".task-card .spinner")).toBeNull();
    const traceLabel = screen.getByText(/完整 Trace（1 条事件）/);
    expect(traceLabel.closest(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    await user.click(traceLabel);
    expect(await screen.findByText("读取文件")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /全部运行/ }));
    await waitFor(() => expect(mocks.runAllBenchmark).toHaveBeenCalledWith("llm"));
    expect(await screen.findByText("状态：未通过")).toBeInTheDocument();
  });

  it("shows an empty state after loading no tasks", async () => {
    mocks.listTasks.mockResolvedValueOnce([]);
    render(<BenchmarkPage />);
    expect(await screen.findByText("暂无可运行的基准任务。")).toBeInTheDocument();
  });
});
