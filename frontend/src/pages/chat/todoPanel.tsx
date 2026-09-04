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

function eventTool(event: ToolEvent): string {
  return String(event.data?.tool ?? event.data?.name ?? event.message ?? "");
}

function eventCallId(event: ToolEvent): string {
  return typeof event.data?.call_id === "string" ? event.data.call_id : "";
}

function parseTodoItem(raw: unknown): TodoItem | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const item = raw as Record<string, unknown>;
  if (!hasExactKeys(item, ["id", "content", "status"])) return null;
  if (typeof item.id !== "string" || !/^todo_[0-9a-f]{32}$/.test(item.id)) return null;
  if (typeof item.content !== "string" || !item.content.trim() || item.content.length > 500) return null;
  if (typeof item.status !== "string" || !TODO_STATUSES.includes(item.status as TodoStatus)) return null;
  return { id: item.id, content: item.content, status: item.status as TodoStatus };
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  return actual.length === expected.length && actual.every((key, index) => key === [...expected].sort()[index]);
}

function validAppliedOperation(raw: unknown): boolean {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return false;
  const operation = raw as Record<string, unknown>;
  const validId = typeof operation.id === "string" && /^todo_[0-9a-f]{32}$/.test(operation.id);
  const validContent = typeof operation.content === "string"
    && Boolean(operation.content.trim())
    && operation.content.length <= 500;
  const validStatus = typeof operation.status === "string"
    && TODO_STATUSES.includes(operation.status as TodoStatus);
  if (operation.op === "add") {
    return hasExactKeys(operation, ["op", "id", "content", "status"])
      && validId && validContent && validStatus;
  }
  if (operation.op === "remove") {
    return hasExactKeys(operation, ["op", "id"]) && validId;
  }
  if (operation.op !== "update" || !validId) return false;
  const keys = Object.keys(operation);
  if (!keys.every((key) => ["op", "id", "content", "status"].includes(key))) return false;
  if (keys.length < 3 || ("content" in operation && !validContent) || ("status" in operation && !validStatus)) {
    return false;
  }
  return "content" in operation || "status" in operation;
}

function parseSnapshot(event: ToolEvent, activeTurnId: string): TodoItem[] | null {
  const raw = typeof event.data?.content === "string" ? event.data.content : event.message;
  try {
    const snapshot = JSON.parse(raw) as Record<string, unknown>;
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return null;
    if (!hasExactKeys(snapshot, ["turn_id", "revision", "applied_operations", "counts", "todos"])) return null;
    if (snapshot.turn_id !== activeTurnId) return null;
    if (!Number.isInteger(snapshot.revision) || Number(snapshot.revision) < 1) return null;
    if (!Array.isArray(snapshot.todos) || snapshot.todos.length > 100) return null;
    const todos = snapshot.todos.map(parseTodoItem);
    if (todos.some((todo) => todo === null)) return null;
    const normalized = todos as TodoItem[];
    if (new Set(normalized.map((todo) => todo.id)).size !== normalized.length) return null;
    if (!Array.isArray(snapshot.applied_operations)
      || snapshot.applied_operations.length < 1
      || snapshot.applied_operations.length > 100
      || !snapshot.applied_operations.every(validAppliedOperation)) return null;
    const counts = snapshot.counts;
    if (!counts || typeof counts !== "object" || Array.isArray(counts)) return null;
    const countRecord = counts as Record<string, unknown>;
    if (!hasExactKeys(countRecord, [...TODO_STATUSES])) return null;
    for (const status of TODO_STATUSES) {
      if (!Number.isInteger(countRecord[status])
        || countRecord[status] !== normalized.filter((todo) => todo.status === status).length) return null;
    }
    return normalized;
  } catch {
    return null;
  }
}

/** Return only the latest successful authoritative snapshot from the active Turn. */
export function latestTodoList(messages: ChatMessage[], activeTurnId?: string): TodoItem[] | null {
  if (!activeTurnId) return null;
  const successfulCalls = new Set<string>();
  let latest: TodoItem[] | null = null;
  for (const message of messages) {
    if (message.role !== "assistant" || message.sourceNodeId !== activeTurnId) continue;
    for (const event of message.events ?? []) {
      const callId = eventCallId(event);
      if (
        event.kind === "tool_call"
        && eventTool(event) === "update_todo_list"
        && event.data?.status === "success"
        && callId
      ) {
        successfulCalls.add(callId);
        continue;
      }
      if (
        event.kind === "tool_result"
        && eventTool(event) === "update_todo_list"
        && event.data?.status === "success"
        && successfulCalls.has(callId)
      ) {
        const snapshot = parseSnapshot(event, activeTurnId);
        if (snapshot !== null) latest = snapshot;
      }
    }
  }
  return latest;
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
                    key={item.id}
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
