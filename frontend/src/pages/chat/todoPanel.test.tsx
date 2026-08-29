import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ChatMessage, ToolEvent } from "../../types";
import { SessionTodoPanel, latestTodoList } from "./todoPanel";

function message(events: ToolEvent[]): ChatMessage {
  return { id: "msg", role: "assistant", content: "", events };
}

function todoCall(todos: unknown[], tool: string | undefined, messageTool: string): ToolEvent {
  const data: Record<string, unknown> = { call_id: "call-1", arguments: { todos } };
  if (tool !== undefined) data.tool = tool;
  return { kind: "tool_call", message: messageTool, data };
}

afterEach(cleanup);

describe("latestTodoList", () => {
  it("extracts the todos from the node-protocol tool_call shape", () => {
    const messages = [
      message([
        todoCall(
          [{ content: "探索仓库", status: "in_progress" }, { content: "写测试", status: "pending" }],
          "todo_write",
          "todo_write",
        ),
      ]),
    ];

    expect(latestTodoList(messages)).toEqual([
      { content: "探索仓库", status: "in_progress" },
      { content: "写测试", status: "pending" },
    ]);
  });

  it("extracts the todos from the legacy event shape using the message field", () => {
    const messages = [
      message([
        todoCall([{ content: "旧列表", status: "completed" }], undefined, "todo_write"),
      ]),
    ];

    expect(latestTodoList(messages)).toEqual([{ content: "旧列表", status: "completed" }]);
  });

  it("uses the newest todo_write across messages and events", () => {
    const messages = [
      message([todoCall([{ content: "第一版", status: "pending" }], "todo_write", "todo_write")]),
      message([
        todoCall([{ content: "其他工具", status: "completed" }], "read_file", "read_file"),
        todoCall(
          [{ content: "第二版", status: "in_progress" }, { content: "第三项", status: "pending" }],
          "todo_write",
          "todo_write",
        ),
      ]),
    ];

    expect(latestTodoList(messages)).toEqual([
      { content: "第二版", status: "in_progress" },
      { content: "第三项", status: "pending" },
    ]);
  });

  it("ignores non-todo tool calls and returns null without any todo_write", () => {
    const messages = [message([todoCall([{ content: "x", status: "pending" }], "grep", "grep")])];

    expect(latestTodoList(messages)).toBeNull();
    expect(latestTodoList([])).toBeNull();
  });

  it("skips malformed items but keeps valid ones", () => {
    const messages = [
      message([
        todoCall(
          [
            { content: "有效", status: "pending" },
            { content: "   ", status: "pending" },
            { content: "坏状态", status: "blocked" },
            "not-an-object",
            { status: "completed" },
          ],
          "todo_write",
          "todo_write",
        ),
      ]),
    ];

    expect(latestTodoList(messages)).toEqual([{ content: "有效", status: "pending" }]);
  });

  it("returns an empty list for an empty todos array", () => {
    const messages = [message([todoCall([], "todo_write", "todo_write")])];

    expect(latestTodoList(messages)).toEqual([]);
  });
});

describe("SessionTodoPanel", () => {
  it("renders progress counts and status icons with the list expanded while busy", () => {
    render(
      <SessionTodoPanel
        busy
        todos={[
          { content: "探索仓库", status: "completed" },
          { content: "实现工具", status: "in_progress" },
          { content: "写测试", status: "pending" },
        ]}
      />,
    );

    expect(screen.getByText("任务清单")).toBeTruthy();
    expect(screen.getByText("1/3 完成")).toBeTruthy();
    expect(screen.getByLabelText("check-circle")).toBeTruthy();
    expect(screen.getByLabelText("loading")).toBeTruthy();
    expect(screen.getByLabelText("clock-circle")).toBeTruthy();
    expect(screen.getByText("探索仓库")).toBeTruthy();
    const active = screen.getByText("实现工具").closest("li");
    expect(active?.className).toContain("is-active");
  });

  it("renders collapsed when idle and expands on header click", () => {
    render(
      <SessionTodoPanel
        busy={false}
        todos={[{ content: "唯一任务", status: "pending" }]}
      />,
    );

    const content = screen.queryByText("唯一任务");
    expect(content).toBeNull();

    const header = screen.getByText("任务清单").closest(".ant-collapse-header");
    expect(header).not.toBeNull();
    fireEvent.click(header!);
    expect(screen.getByText("唯一任务")).toBeTruthy();
  });
});
