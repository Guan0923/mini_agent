import { Collapse } from "antd";
import { CheckCircleTwoTone, ClockCircleTwoTone, CloseOutlined, LoadingOutlined } from "@ant-design/icons";
import { useState } from "react";
import IconAction from "../../components/IconAction";
import type { ChatMessage, TodoItem, TodoStatus, ToolEvent } from "../../types";

const TODO_STATUSES: readonly TodoStatus[] = ["pending", "in_progress", "completed"];

function StatusIcon({ status }: { status: TodoStatus }) {
  if (status === "completed") return <CheckCircleTwoTone twoToneColor="#52c41a" />;
  if (status === "in_progress") return <LoadingOutlined spin />;
  return <ClockCircleTwoTone twoToneColor="#bfbfbf" />;
}

function isTodoWriteEvent(event: ToolEvent): boolean {
  return event.kind === "tool_call" && String(event.data?.tool ?? event.message ?? "") === "todo_write";
}

function parseTodoItem(raw: unknown): TodoItem | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const item = raw as Record<string, unknown>;
  if (typeof item.content !== "string" || !item.content.trim()) return null;
  if (typeof item.status !== "string" || !TODO_STATUSES.includes(item.status as TodoStatus)) return null;
  return { content: item.content, status: item.status as TodoStatus };
}

/** Return the session's latest todo list by scanning messages newest-first. */
export function latestTodoList(messages: ChatMessage[]): TodoItem[] | null {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
    const events = messages[messageIndex].events ?? [];
    for (let eventIndex = events.length - 1; eventIndex >= 0; eventIndex -= 1) {
      const event = events[eventIndex];
      if (!isTodoWriteEvent(event)) continue;
      const argumentsValue = event.data?.arguments as Record<string, unknown> | undefined;
      const todos = argumentsValue?.todos;
      if (!Array.isArray(todos)) return null;
      return todos.map(parseTodoItem).filter((item): item is TodoItem => Boolean(item));
    }
  }
  return null;
}

export function SessionTodoPanel({
  todos,
  busy,
  closable = false,
  onClose,
}: {
  todos: TodoItem[];
  busy: boolean;
  closable?: boolean;
  onClose?: () => void;
}) {
  // null means "follow the default rule": expanded while running, collapsed when idle.
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const completed = todos.filter((item) => item.status === "completed").length;
  const total = todos.length;
  const open = userOpen ?? busy;
  return (
    <Collapse
      className="todo-panel"
      size="small"
      items={[
        {
          key: "todo",
          label: (
            <span className="todo-header">
              任务清单
              <span className="todo-header-count">
                {completed}/{total} 完成
              </span>
            </span>
          ),
          extra: closable && onClose ? (
            <IconAction
              className="todo-panel-close"
              label="关闭任务清单"
              icon={<CloseOutlined />}
              onClick={(event) => {
                event.stopPropagation();
                onClose();
              }}
            />
          ) : null,
          children: (
            <div className="todo-body">
              <ul className="todo-list">
                {todos.map((item) => (
                  <li
                    key={item.content}
                    className={`todo-item${item.status === "in_progress" && busy ? " is-active" : ""}`}
                  >
                    <span className="todo-icon">
                      <StatusIcon status={item.status} />
                    </span>
                    <span className="todo-content">{item.content}</span>
                  </li>
                ))}
              </ul>
            </div>
          ),
        },
      ]}
      activeKey={open ? ["todo"] : []}
      onChange={(key) => setUserOpen(Array.isArray(key) ? key.includes("todo") : key === "todo")}
    />
  );
}
