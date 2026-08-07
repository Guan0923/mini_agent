import { Alert, Avatar, Collapse, App as AntApp, message as staticMessage } from "antd";
import { BranchesOutlined, CloseCircleOutlined, CopyOutlined, EditOutlined, FileTextOutlined, RollbackOutlined, ToolOutlined } from "@ant-design/icons";
import type { ChatMessage, DecisionRequest, DisplayMode, ToolEvent } from "../../types";
import DecisionCard from "../../components/DecisionCard";
import IconAction from "../../components/IconAction";
import MarkdownContent from "../../components/MarkdownContent";

export async function copyText(value: string): Promise<void> {
  const clipboard = typeof window !== "undefined" ? window.navigator.clipboard : navigator.clipboard;
  if (clipboard?.writeText) {
    await clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("浏览器拒绝了复制操作");
}

export function MessageActions({
  msg,
  busy,
  onFork,
  onRewind,
  onEdit,
}: {
  msg: ChatMessage;
  busy: boolean;
  onFork?: () => void;
  onRewind?: () => void;
  onEdit?: () => void;
}) {
  const { message: contextMessage } = AntApp.useApp();
  const message = contextMessage && typeof contextMessage.success === "function" ? contextMessage : staticMessage;
  async function copy() {
    if (!msg.content) return;
    try { await copyText(msg.content); message.success("已复制"); } catch { message.error("复制失败"); }
  }
  return (
    <div className="message-actions" aria-label={`${msg.role === "user" ? "用户" : "Agent"}消息操作`}>
      <IconAction label="复制" icon={<CopyOutlined />} onClick={() => void copy()} disabled={!msg.content} />
      {onRewind ? <IconAction label="回溯" icon={<RollbackOutlined />} onClick={onRewind} disabled={busy} /> : null}
      {onEdit ? <IconAction label="编辑" icon={<EditOutlined />} onClick={onEdit} disabled={busy || !msg.content} /> : null}
      {onFork ? <IconAction label="Fork" icon={<BranchesOutlined />} onClick={onFork} disabled={busy || msg.running || !msg.content} /> : null}
    </div>
  );
}

export function ToolLine({ ev, display }: { ev: ToolEvent; display: DisplayMode }) {
  if (display === "minimal") return null;
  if (ev.kind === "tool_call") {
    const args = ev.data?.arguments;
    const shown = typeof args === "string" ? args : JSON.stringify(args ?? "");
    return <div className="tool-line"><ToolOutlined aria-hidden="true" /><b>{ev.message}</b>{display === "verbose" ? <span className="mono">{shown}</span> : null}</div>;
  }
  if (ev.kind === "tool_failed") return <Alert className="tool-line failed" type="error" showIcon icon={<CloseCircleOutlined />} title={ev.message} />;
  if (ev.kind === "tool_result") {
    const result = (ev.data?.result as string | undefined) ?? ev.message;
    return <Collapse className="tool-result" ghost defaultActiveKey={display === "verbose" ? ["result"] : []} items={[{ key: "result", label: <><FileTextOutlined /> {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</>, children: <pre>{result}</pre> }]} />;
  }
  return null;
}

export function AssistantMessage({
  msg,
  display,
  onDecision,
  busy,
  onFork,
}: {
  msg: ChatMessage;
  display: DisplayMode;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
  busy: boolean;
  onFork?: () => void;
}) {
  return (
    <div className="message assistant">
      <Avatar className="avatar" size={32}>A</Avatar>
      <div className="bubble">
        {msg.events.length > 0 && display !== "minimal" ? <div className="event-list">{msg.events.map((ev, index) => <ToolLine key={index} ev={ev} display={display} />)}</div> : null}
        {msg.decision ? <DecisionCard request={msg.decision} onSubmit={(choice, options) => onDecision(msg.decision!, choice, options)} /> : null}
        {msg.error ? <Alert className="error-text" type="error" showIcon title={`⚠️ ${msg.error}`} /> : msg.content ? <MarkdownContent text={msg.content} /> : msg.running && !msg.decision && display !== "minimal" ? <div className="thinking" role="status" aria-label="思考中" data-state="thinking" aria-live="polite"><span className="dot" /><span className="dot" /><span className="dot" /></div> : null}
        {display !== "minimal" && (msg.status || (msg.metrics && msg.metrics.duration_ms != null)) ? <div className="meta">{msg.status ?? ""}{msg.status && msg.metrics && msg.metrics.duration_ms != null ? " · " : ""}{msg.metrics && msg.metrics.duration_ms != null ? `${(msg.metrics.duration_ms / 1000).toFixed(1)}s · ${msg.metrics.model_calls ?? 0} 次模型调用 · ${msg.metrics.tool_calls ?? 0} 次工具调用` : null}</div> : null}
        <MessageActions msg={msg} busy={busy} onFork={onFork} />
      </div>
    </div>
  );
}
