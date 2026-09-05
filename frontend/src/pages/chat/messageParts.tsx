import { Alert, BorderBeam, Collapse, App as AntApp, message as staticMessage } from "antd";
import { BranchesOutlined, CopyOutlined, EditOutlined, FileTextOutlined, ToolOutlined } from "@ant-design/icons";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage, DecisionRequest, DisplayMode, FileReference, ToolEvent, TurnItem } from "../../types";
import { fileSourceLabels } from "../../types/files";
import { effectiveDisplayMode } from "../../app/displayMode";
import { fileReferenceAvailable, sessionFileContentUrl } from "../../api";
import DecisionCard from "../../components/DecisionCard";
import IconAction from "../../components/IconAction";
import MarkdownContent from "../../components/MarkdownContent";
import ShimmerText from "../../components/ShimmerText";
import AssistantIcon from "../../components/AssistantIcon";

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

/** One structured file reference chip with an availability probe. */
export function MessageReferenceChip({
  reference,
  sessionId,
}: {
  reference: FileReference;
  sessionId?: string;
}) {
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    if (!sessionId) {
      setAvailable(false);
      return;
    }
    let disposed = false;
    void fileReferenceAvailable(reference, sessionId).then((ok) => {
      if (!disposed) setAvailable(ok);
    });
    return () => {
      disposed = true;
    };
  }, [reference.source, reference.path, sessionId]);

  const url = sessionId ? sessionFileContentUrl(sessionId, reference.source, reference.path) : undefined;
  const sourceLabel = fileSourceLabels[reference.source];
  if (available === false) {
    return (
      <span className="message-reference is-unavailable" title="文件不可用">
        <span className={`file-source-badge ${reference.source}`}>{sourceLabel}</span>
        <span className="message-reference-path" title={reference.display_path}>{reference.display_path}</span>
        <span className="message-reference-unavailable">文件不可用</span>
      </span>
    );
  }
  return (
    <a
      className="message-reference"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => event.stopPropagation()}
      aria-label={`引用 ${reference.display_path}`}
      title={reference.display_path}
    >
      <span className={`file-source-badge ${reference.source}`}>{sourceLabel}</span>
      <span className="message-reference-path">{reference.display_path}</span>
    </a>
  );
}

export function MessageActions({
  msg,
  busy,
  onFork,
  onEdit,
}: {
  msg: ChatMessage;
  busy: boolean;
  onFork?: () => void;
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
      {onEdit ? <IconAction label="编辑" icon={<EditOutlined />} onClick={onEdit} disabled={busy || !msg.content} /> : null}
      {onFork ? <IconAction label="Fork" icon={<BranchesOutlined />} onClick={onFork} disabled={busy || msg.running || !msg.content} /> : null}
    </div>
  );
}

function jsonText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

export function summarizeReasoningTail(value: string, limit = 250): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  const segmenterConstructor = (Intl as unknown as {
    Segmenter?: new (locale?: string, options?: { granularity: "grapheme" }) => {
      segment(input: string): Iterable<{ segment: string }>;
    };
  }).Segmenter;
  const characters = segmenterConstructor
    ? Array.from(new segmenterConstructor(undefined, { granularity: "grapheme" }).segment(normalized), (item) => item.segment)
    : Array.from(normalized);
  return characters.length > limit ? `…${characters.slice(-limit).join("")}` : normalized;
}

function callId(ev: ToolEvent): string {
  return typeof ev.data?.call_id === "string" ? ev.data.call_id : "";
}

function isUserDenied(value: { failure_code?: unknown }): boolean {
  return value.failure_code === "user_denied";
}

export function ToolLine({ ev, display, active = false }: { ev: ToolEvent; display: DisplayMode; active?: boolean }) {
  const denied = ev.kind === "tool_failed" && isUserDenied(ev.data ?? {});
  if (denied) {
    const tool = String(ev.data?.tool ?? "工具");
    return (
      <div className="tool-line">
        <ToolOutlined aria-hidden="true" />
        <b>{tool}</b>
        <span className="tool-status failed">已拒绝</span>
        {display === "developer" && callId(ev) ? <span className="tool-call-id">call ID: {callId(ev)}</span> : null}
      </div>
    );
  }
  if (ev.kind === "tool_failed") {
    const tool = String(ev.data?.tool ?? "工具");
    return (
      <div className="tool-line failed">
        <ToolOutlined aria-hidden="true" />
        <b>{tool}</b>
        <span className="tool-status failed">失败</span>
        {display === "developer" && callId(ev) ? <span className="tool-call-id">call ID: {callId(ev)}</span> : null}
        {display !== "minimal" ? <pre className="tool-result error-text">{jsonText(ev.data?.result ?? ev.message)}</pre> : null}
        {display === "developer" ? <pre className="tool-payload">{jsonText(ev.data)}</pre> : null}
      </div>
    );
  }
  if (ev.kind === "tool_call") {
    const tool = String(ev.data?.tool ?? ev.message ?? "工具") || "工具";
    return (
      <div className={active ? "tool-line is-active" : "tool-line"}>
        <ToolOutlined aria-hidden="true" />
        <b>{active ? `正在调用 ${tool}` : `调用 ${tool}`}</b>
        {display === "verbose" && !active ? <span className="mono">{jsonText(ev.data?.arguments)}</span> : null}
        {display === "developer" && callId(ev) ? <span className="tool-call-id">call ID: {callId(ev)}</span> : null}
        {display === "developer" ? <pre className="tool-payload">{jsonText(ev.data)}</pre> : null}
      </div>
    );
  }
  if (ev.kind === "tool_result") {
    const result = ev.data?.result ?? ev.message;
    return (
      <div className="tool-result">
        <div className="tool-result-label"><FileTextOutlined /> {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</div>
        {display === "developer" && callId(ev) ? <div className="tool-call-id">call ID: {callId(ev)}</div> : null}
        {display === "minimal" ? null : <pre>{jsonText(result)}</pre>}
        {display === "developer" ? <pre className="tool-payload">{jsonText(ev.data)}</pre> : null}
      </div>
    );
  }
  return null;
}

function runtimeToolName(item: TurnItem): string {
  return String(item.type === "tool_call" ? item.name ?? "工具" : item.tool ?? "工具") || "工具";
}

function runtimeActiveLabel(item: TurnItem, minimal = false): string {
  if (item.type === "reasoning") return minimal ? "思考中" : "正在思考中";
  const tool = runtimeToolName(item);
  return item.type === "tool_call" ? `正在调用 ${tool}` : `正在处理 ${tool} 结果`;
}

function runtimeCompletedLabel(item: TurnItem): string {
  if (item.type === "reasoning") return "思考详情";
  const tool = runtimeToolName(item);
  if (item.type === "tool_call") return `调用 ${tool}`;
  return item.status === "failed" ? `${tool} 失败` : `${tool} 结果`;
}

function RuntimeStatusDots() {
  return (
    <span className="runtime-status-dots" aria-hidden="true">
      <span className="runtime-status-dot" />
      <span className="runtime-status-dot" />
      <span className="runtime-status-dot" />
    </span>
  );
}

function RuntimeStatusLabel({ text, shimmer = false }: { text: string; shimmer?: boolean }) {
  return (
    <span className="runtime-status-label">
      {shimmer ? <ShimmerText active>{text}</ShimmerText> : <span className="runtime-status-text">{text}</span>}
      <RuntimeStatusDots />
    </span>
  );
}

function alignRuntimeSummaryTail(viewport: HTMLSpanElement | null) {
  if (!viewport) return;
  viewport.scrollLeft = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
}

function RuntimeSummaryLabel({ text }: { text: string }) {
  const viewportRef = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    alignRuntimeSummaryTail(viewportRef.current);
  }, [text]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => alignRuntimeSummaryTail(viewport));
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  return (
    <span className="runtime-summary-viewport" ref={viewportRef}>
      <span className="runtime-summary-track">
        <span className="runtime-summary-text">{text}</span>
      </span>
    </span>
  );
}

function runtimeItemLabel(item: TurnItem, expanded: boolean, active: boolean) {
  if (item.type !== "reasoning") {
    const label = active ? runtimeActiveLabel(item) : runtimeCompletedLabel(item);
    return active ? <RuntimeStatusLabel text={label} shimmer={!expanded} /> : <span className="runtime-static-label">{label}</span>;
  }
  if (expanded) {
    return active
      ? <RuntimeStatusLabel text={runtimeActiveLabel(item)} />
      : <span className="runtime-static-label">{runtimeCompletedLabel(item)}</span>;
  }
  const summary = summarizeReasoningTail(String(item.text ?? ""));
  if (summary) {
    return <RuntimeSummaryLabel text={summary} />;
  }
  return active
    ? <RuntimeStatusLabel text={runtimeActiveLabel(item)} shimmer />
    : <span className="runtime-static-label">{runtimeCompletedLabel(item)}</span>;
}

function runtimeItemBody(item: TurnItem, display: DisplayMode, active: boolean) {
  if (item.type === "reasoning") {
    const raw = String(item.text ?? "");
    return (
      <div className="thinking-content">
        <MarkdownContent text={raw || "正在思考…"} />
      </div>
    );
  }
  const failed = item.type === "tool_result" && item.status === "failed";
  const event: ToolEvent = item.type === "tool_call"
    ? { kind: "tool_call", message: String(item.name ?? "工具"), data: { ...item, tool: item.name } }
    : { kind: failed ? "tool_failed" : "tool_result", message: jsonText(item.content), data: { ...item, result: item.content } };
  return <ToolLine ev={event} display={display} active={active && item.type === "tool_call"} />;
}

function RuntimeItemCollapse({
  item,
  itemKey,
  display,
  active,
}: {
  item: TurnItem;
  itemKey: string;
  display: DisplayMode;
  active: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  const label = runtimeItemLabel(item, expanded, active);
  return (
    <Collapse
      className={`runtime-collapse runtime-item-collapse runtime-${item.type.replace("_", "-")}`}
      data-item-type={item.type}
      ghost
      size="small"
      activeKey={expanded ? [itemKey] : []}
      onChange={(keys) => setExpanded(Array.isArray(keys) ? keys.map(String).includes(itemKey) : String(keys) === itemKey)}
      items={[{
        key: itemKey,
        label,
        children: runtimeItemBody(item, display, active),
      }]}
    />
  );
}

function MinimalRuntimeStatus({ item }: { item: TurnItem }) {
  const label = runtimeActiveLabel(item, true);
  return (
    <div className="runtime-minimal-status" data-item-type={item.type} role="status" aria-label={label}>
      <span className="runtime-status-text">{label}</span>
      <RuntimeStatusDots />
    </div>
  );
}

function RetryItem({ item, active }: { item: TurnItem; active: boolean }) {
  const attempt = typeof item.attempt === "number" ? item.attempt : 1;
  const maxRetries = typeof item.max_retries === "number" ? item.max_retries : attempt;
  const label = active
    ? `网络异常，正在重试（${attempt}/${maxRetries}）`
    : `网络请求已重试（${attempt}/${maxRetries}）`;
  const message = String(item.message ?? "");
  return (
    <div
      className={`runtime-retry-item${active ? " is-active" : ""}`}
      data-item-type="retry"
      role={active ? "status" : undefined}
      aria-label={label}
      aria-live={active ? "polite" : undefined}
    >
      {active ? <RuntimeStatusLabel text={label} shimmer /> : <span className="runtime-static-label">{label}</span>}
      {message ? <div className="runtime-retry-message">{message}</div> : null}
    </div>
  );
}

const HIDDEN_ASSISTANT_ITEM_TYPES = new Set(["skill_snapshot"]);

function visibleAssistantItems(items: TurnItem[] | undefined): TurnItem[] {
  return (items ?? []).filter((item) => !HIDDEN_ASSISTANT_ITEM_TYPES.has(item.type));
}

function OrderedAssistantItems({
  msg,
  items,
  configuredDisplay,
  onDecision,
}: {
  msg: ChatMessage;
  items: TurnItem[];
  configuredDisplay: DisplayMode;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
}) {
  const display = effectiveDisplayMode(configuredDisplay);
  const version = msg.itemVersion ?? 0;
  return (
    <div className="runtime-items">
      {msg.compactionNotice ? <div className="runtime-compaction-notice">上下文已压缩</div> : null}
      {items.map((item, index) => {
        const identity = `${msg.id}:${version}:${index}`;
        const active = Boolean(msg.running && index === items.length - 1);
        if (item.type === "retry") {
          return <RetryItem key={identity} item={item} active={active && item.status === "running"} />;
        }
        if (["reasoning", "tool_call", "tool_result"].includes(item.type)) {
          if (display === "minimal") return active ? <MinimalRuntimeStatus key={identity} item={item} /> : null;
          return <RuntimeItemCollapse key={identity} item={item} itemKey={identity} display={display} active={active} />;
        }
        if (item.type === "text" || item.type === "bash") {
          const value = String(item.text ?? "");
          return value ? <div className="runtime-item-response" data-item-type={item.type} key={identity}><MarkdownContent text={value} /></div> : null;
        }
        if (item.type === "error") {
          return <Alert key={identity} className="error-text" type="error" showIcon title={String(item.message ?? "Execution failed.")} />;
        }
        if (item.type === "subagent" && item.event === "agent_report") {
          const value = String(item.text ?? "");
          const failed = item.report_status === "failed";
          return value ? (
            <div className={`runtime-agent-report${failed ? " failed" : ""}`} data-item-type="subagent" data-report-status={failed ? "failed" : "success"} key={identity}>{value}</div>
          ) : null;
        }
        const decision = msg.decision;
        if (decision && (item.type === "approval" || item.type === "question") && decision.decision_id === item.decision_id) {
          return (
            <div data-item-type={item.type} key={identity}>
              <DecisionCard request={decision} onSubmit={(choice, options) => onDecision(decision, choice, options)} />
            </div>
          );
        }
        if (item.type === "approval") {
          if (item.event !== "approval_resolved" || !["allowed", "denied"].includes(String(item.approval_status))) return null;
          const denied = item.approval_status === "denied";
          const tool = String(item.tool ?? "工具");
          return (
            <div
              className={`tool-line runtime-approval-status${denied ? " failed" : ""}`}
              data-item-type="approval"
              key={identity}
            >
              <ToolOutlined aria-hidden="true" />
              <span>{`${denied ? "已拒绝" : "已允许"} ${tool}`}</span>
            </div>
          );
        }
        const value = String(item.text ?? "");
        return value ? <div className="runtime-business-item" data-item-type={item.type} key={identity}><MarkdownContent text={value} /></div> : null;
      })}
    </div>
  );
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
  const hasItems = msg.items !== undefined;
  const visibleItems = visibleAssistantItems(msg.items);
  const hasDecisionItem = Boolean(msg.decision && visibleItems.some((item) => item.decision_id === msg.decision?.decision_id));
  const hasErrorItem = visibleItems.some((item) => item.type === "error");
  const frame = (
    <div className={msg.running ? "assistant-run-frame is-running" : "assistant-run-frame"}>
      {hasItems ? <OrderedAssistantItems msg={msg} items={visibleItems} configuredDisplay={display} onDecision={onDecision} /> : null}
      {!hasDecisionItem && msg.decision ? <DecisionCard request={msg.decision} onSubmit={(choice, options) => onDecision(msg.decision!, choice, options)} /> : null}
      {!hasErrorItem && msg.error ? <Alert className="error-text" type="error" showIcon title={msg.error} /> : null}
      {!hasItems && msg.content ? <MarkdownContent text={msg.content} /> : null}
      {!msg.error && (!hasItems || visibleItems.length === 0) && !msg.content && msg.running && !msg.decision ? <div className="thinking" role="status" aria-label="思考中" data-state="thinking" aria-live="polite"><span className="dot" /><span className="dot" /><span className="dot" /></div> : null}
      {display !== "minimal" && (msg.status || (msg.metrics && msg.metrics.duration_ms != null)) ? <div className="meta">{msg.status ?? ""}{msg.status && msg.metrics && msg.metrics.duration_ms != null ? " · " : ""}{msg.metrics && msg.metrics.duration_ms != null ? `${(msg.metrics.duration_ms / 1000).toFixed(1)}s · ${msg.metrics.model_calls ?? 0} 次模型调用 · ${msg.metrics.tool_calls ?? 0} 次工具调用` : null}</div> : null}
      <MessageActions msg={msg} busy={busy} onFork={onFork} />
    </div>
  );
  return (
    <div className="message assistant">
      <AssistantIcon className="assistant-icon" />
      <div className="bubble">
        {msg.running ? <BorderBeam>{frame}</BorderBeam> : frame}
      </div>
    </div>
  );
}
