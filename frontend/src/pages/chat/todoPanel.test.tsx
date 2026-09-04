import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, TodoItem, ToolEvent } from "../../types";
import { SessionTodoPanel, latestTodoList } from "./todoPanel";

const TURN_ID = "turn-current";
const TODO_ONE = "todo_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TODO_TWO = "todo_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

function message(events: ToolEvent[], sourceNodeId = TURN_ID): ChatMessage {
  return { id: crypto.randomUUID(), role: "assistant", content: "", events, sourceNodeId };
}

function todoCall(callId: string, status = "success"): ToolEvent {
  return {
    kind: "tool_call",
    message: "update_todo_list",
    data: { call_id: callId, name: "update_todo_list", status },
  };
}

function todoResult(callId: string, todos: unknown[], options: { status?: string; turnId?: string } = {}): ToolEvent {
  const status = options.status ?? "success";
  const counts = { pending: 0, in_progress: 0, completed: 0 };
  for (const todo of todos) {
    if (!todo || typeof todo !== "object" || Array.isArray(todo)) continue;
    const todoStatus = (todo as Record<string, unknown>).status;
    if (todoStatus === "pending" || todoStatus === "in_progress" || todoStatus === "completed") counts[todoStatus] += 1;
  }
  return {
    kind: status === "success" ? "tool_result" : "tool_failed",
    message: JSON.stringify({
      turn_id: options.turnId ?? TURN_ID,
      revision: 1,
      applied_operations: [{ op: "remove", id: TODO_ONE }],
      counts,
      todos,
    }),
    data: { call_id: callId, tool: "update_todo_list", status },
  };
}

afterEach(cleanup);

describe("latestTodoList", () => {
  it("uses a matching successful call/result pair from the active Turn", () => {
    const todos = [
      { id: TODO_ONE, content: "探索仓库", status: "in_progress" },
      { id: TODO_TWO, content: "写测试", status: "pending" },
    ];

    expect(latestTodoList([message([todoCall("call-1"), todoResult("call-1", todos)])], TURN_ID)).toEqual(todos);
  });

  it("uses the newest valid authoritative snapshot", () => {
    const first = [{ id: TODO_ONE, content: "第一版", status: "pending" }];
    const second = [{ id: TODO_ONE, content: "第二版", status: "completed" }];
    const messages = [
      message([todoCall("call-1"), todoResult("call-1", first)]),
      message([todoCall("call-2"), todoResult("call-2", second)]),
    ];

    expect(latestTodoList(messages, TURN_ID)).toEqual(second);
  });

  it("ignores call arguments, failed results, missing pairs, and ancestor Turns", () => {
    const forgedCall = todoCall("call-forged");
    forgedCall.data!.arguments = {
      todos: [{ id: TODO_ONE, content: "调用参数不能显示", status: "pending" }],
    };
    const failed = todoResult(
      "call-failed",
      [{ id: TODO_ONE, content: "失败结果", status: "pending" }],
      { status: "failed" },
    );
    const ancestor = message(
      [
        todoCall("call-old"),
        todoResult(
          "call-old",
          [{ id: TODO_ONE, content: "旧 Turn", status: "pending" }],
          { turnId: "turn-old" },
        ),
      ],
      "turn-old",
    );

    expect(latestTodoList([ancestor, message([forgedCall, failed])], TURN_ID)).toBeNull();
  });

  it("rejects the whole malformed snapshot instead of keeping valid rows", () => {
    const malformed = [
      { id: TODO_ONE, content: "有效", status: "pending" },
      { id: "bad", content: "无效", status: "pending" },
    ];

    expect(latestTodoList([message([todoCall("call-1"), todoResult("call-1", malformed)])], TURN_ID)).toBeNull();
  });

  it("rejects incomplete result metadata and count mismatches", () => {
    const todos = [{ id: TODO_ONE, content: "有效", status: "pending" }];
    const incomplete = todoResult("call-1", todos);
    incomplete.message = JSON.stringify({ turn_id: TURN_ID, revision: 1, todos });
    const mismatched = todoResult("call-2", todos);
    const parsed = JSON.parse(mismatched.message);
    parsed.counts.pending = 0;
    mismatched.message = JSON.stringify(parsed);

    expect(latestTodoList([message([todoCall("call-1"), incomplete])], TURN_ID)).toBeNull();
    expect(latestTodoList([message([todoCall("call-2"), mismatched])], TURN_ID)).toBeNull();
  });

  it("accepts an authoritative empty snapshot and requires an active Turn", () => {
    const messages = [message([todoCall("call-1"), todoResult("call-1", [])])];

    expect(latestTodoList(messages, TURN_ID)).toEqual([]);
    expect(latestTodoList(messages)).toBeNull();
  });
});

describe("SessionTodoPanel", () => {
  const todos: TodoItem[] = [
    { id: TODO_ONE, content: "相同内容", status: "completed" },
    { id: TODO_TWO, content: "相同内容", status: "in_progress" },
    { id: "todo_cccccccccccccccccccccccccccccccc", content: "写测试", status: "pending" },
  ];

  it("renders duplicate content with progress and status icons while busy", () => {
    render(<SessionTodoPanel busy todos={todos} />);

    expect(screen.getByText("任务清单")).toBeTruthy();
    expect(screen.getByText("1/3 完成")).toBeTruthy();
    expect(screen.getByLabelText("check-circle")).toBeTruthy();
    expect(screen.getByLabelText("loading")).toBeTruthy();
    expect(screen.getByLabelText("clock-circle")).toBeTruthy();
    expect(screen.getAllByText("相同内容")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "关闭任务清单" })).toBeNull();
  });

  it("renders collapsed with a close action when an incomplete Turn is idle", () => {
    const onClose = vi.fn();
    render(
      <SessionTodoPanel
        busy={false}
        closable
        onClose={onClose}
        todos={[{ id: TODO_ONE, content: "唯一任务", status: "pending" }]}
      />,
    );

    expect(screen.queryByText("唯一任务")).toBeNull();
    const header = screen.getByText("任务清单").closest(".ant-collapse-header");
    fireEvent.click(screen.getByRole("button", { name: "关闭任务清单" }));
    expect(onClose).toHaveBeenCalledOnce();
    fireEvent.click(header!);
    expect(screen.getByText("唯一任务")).toBeTruthy();
  });
});
