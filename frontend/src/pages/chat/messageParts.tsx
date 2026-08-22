import { Alert, BorderBeam, Collapse, App as AntApp, message as staticMessage } from "antd";
import { BranchesOutlined, CopyOutlined, EditOutlined, FileTextOutlined, RollbackOutlined, ToolOutlined } from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import type { ChatMessage, DecisionRequest, DisplayMode, FileReference, RunPresentationSegment, RunPresentationTool, ToolEvent } from "../../types";
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
  const label = reference.source === "upload" ? "会话上传" : "项目文件";
  if (available === false) {
    return (
      <span className="message-reference is-unavailable" title="文件不可用">
        <span className={`file-source-badge ${reference.source}`}>{label}</span>
        <span className="message-reference-path">{reference.path}</span>
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
      aria-label={`引用 ${reference.path}`}
    >
      <span className={`file-source-badge ${reference.source}`}>{label}</span>
      <span className="message-reference-path">{reference.path}</span>
    </a>
  );
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

function jsonText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

export function summarizeThinking(value: string, limit = 100): string {
  const paragraph = value
    .split(/\r?\n\s*\r?\n/)
    .map((item) => item.trim())
    .find(Boolean) ?? "";
  const segmenterConstructor = (Intl as unknown as {
    Segmenter?: new (locale?: string, options?: { granularity: "grapheme" }) => {
      segment(input: string): Iterable<{ segment: string }>;
    };
  }).Segmenter;
  const characters = segmenterConstructor
    ? Array.from(new segmenterConstructor(undefined, { granularity: "grapheme" }).segment(paragraph), (item) => item.segment)
    : Array.from(paragraph);
  return characters.length > limit ? `${characters.slice(0, limit).join("")}.....` : paragraph;
}

function callId(ev: ToolEvent): string {
  return typeof ev.data?.call_id === "string" ? ev.data.call_id : "";
}

function isUserDenied(value: { failure_code?: unknown }): boolean {
  return value.failure_code === "user_denied";
}

function isHiddenRecoverableToolFailure(value: { failure_code?: unknown }): boolean {
  return value.failure_code === "user_denied_batch";
}

export function ToolLine({ ev, display, active = false }: { ev: ToolEvent; display: DisplayMode; active?: boolean }) {
  const denied = ev.kind === "tool_failed" && isUserDenied(ev.data ?? {});
  if (display === "minimal" && !denied) return null;
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
    const result = (ev.data?.result as string | undefined) ?? ev.message;
    return (
      <div className="tool-result">
        <div className="tool-result-label"><FileTextOutlined /> {ev.data?.tool ? String(ev.data.tool) : "工具"} 结果</div>
        {display === "developer" && callId(ev) ? <div className="tool-call-id">call ID: {callId(ev)}</div> : null}
        <pre>{jsonText(result)}</pre>
        {display === "developer" ? <pre className="tool-payload">{jsonText(ev.data)}</pre> : null}
      </div>
    );
  }
  return null;
}

function RuntimeDetails({ msg, configuredDisplay }: { msg: ChatMessage; configuredDisplay: DisplayMode }) {
  const display = effectiveDisplayMode(configuredDisplay);
  const [activeKey, setActiveKey] = useState<string[]>(msg.running ? ["details"] : []);
  const previousRunning = useRef(Boolean(msg.running));
  const thinking = msg.events.filter((event) => event.kind === "thinking").map((event) => event.message).filter(Boolean).join("\n\n");
  const toolEvents = msg.events.filter((event) => ["tool_call", "tool_result", "tool_failed"].includes(event.kind));
  const finishedCallIds = new Set(toolEvents.filter((event) => event.kind !== "tool_call").map(callId).filter(Boolean));
  const activeCalls = toolEvents.filter((event) => event.kind === "tool_call" && (!callId(event) || !finishedCallIds.has(callId(event))));
  const showAllTools = display === "verbose" || display === "developer";
  const shownTools = display === "developer"
    ? toolEvents
    : msg.running
      ? activeCalls
      : showAllTools
        ? toolEvents
        : toolEvents.filter((event) => event.kind === "tool_failed" && isUserDenied(event.data ?? {}));
  const hasDetails = Boolean(thinking || shownTools.length > 0 || msg.running);

  useEffect(() => {
    if (msg.running && !previousRunning.current) setActiveKey(["details"]);
    if (!msg.running && previousRunning.current) setActiveKey([]);
    previousRunning.current = Boolean(msg.running);
  }, [msg.running]);

  if (!hasDetails) return null;
  const thought = display === "minimal" ? summarizeThinking(thinking) : thinking;
  const thoughtContent = thought || (msg.running ? "正在思考…" : "未返回思考内容");

  return (
    <Collapse
      className="runtime-collapse runtime-details"
      ghost
      size="small"
      activeKey={activeKey}
      onChange={(key) => setActiveKey(Array.isArray(key) ? key.map(String) : [String(key)])}
      items={[{
        key: "details",
        label: msg.running ? "运行中" : "运行详情",
        children: (
          <div className="runtime-details-body">
            <div className="thinking-content">
              {display === "minimal"
                ? <ShimmerText active={Boolean(msg.running)}>{thoughtContent}</ShimmerText>
                : <MarkdownContent text={thoughtContent} />}
            </div>
            {shownTools.length > 0 ? (
              <div className="event-list">
                {shownTools.map((event, index) => (
                  <ToolLine
                    key={`${event.kind}-${callId(event) || index}`}
                    ev={event}
                    display={display}
                    active={msg.running && event.kind === "tool_call" && activeCalls.includes(event)}
                  />
                ))}
              </div>
            ) : null}
          </div>
        ),
      }]}
    />
  );
}

function presentationToolBody(tool: RunPresentationTool, display: DisplayMode) {
  const denied = isUserDenied(tool);
  const status = denied ? "已拒绝" : tool.status === "pending" ? "等待执行" : tool.status === "succeeded" ? "已完成" : "失败";
  const developerDetails = denied ? { ...tool, result: undefined, error: undefined } : tool;
  return (
    <div className="runtime-tool-body">
      <div className="tool-line">
        <ToolOutlined aria-hidden="true" /> <b>{tool.name}</b> <span className={`tool-status ${tool.status}`}>{status}</span>
        {display === "developer" ? <span className="tool-call-id">call ID: {tool.call_id}</span> : null}
      </div>
      {display === "verbose" || display === "developer" ? <pre className="tool-payload">{jsonText(tool.arguments)}</pre> : null}
      {tool.result && display !== "minimal" ? <pre className="tool-result">{jsonText(tool.result)}</pre> : null}
      {tool.error && !denied ? <pre className="tool-result error-text">{tool.error}</pre> : null}
      {display === "developer" ? <pre className="tool-payload">{jsonText(developerDetails)}</pre> : null}
    </div>
  );
}

function RunSegments({ msg, configuredDisplay }: { msg: ChatMessage; configuredDisplay: DisplayMode }) {
  const display = effectiveDisplayMode(configuredDisplay);
  const segments = msg.segments ?? [];
  const [activeKeys, setActiveKeys] = useState<string[]>(() =>
    segments.filter((segment) => msg.running && (segment.segment_type === "thinking" || segment.segment_type === "tool_batch"))
      .map((segment) => segment.segment_id),
  );

  useEffect(() => {
    const streaming = segments
      .filter((segment) => segment.status === "streaming" && (segment.segment_type === "thinking" || segment.segment_type === "tool_batch"))
      .map((segment) => segment.segment_id);
    if (streaming.length) setActiveKeys((current) => [...new Set([...current, ...streaming])]);
  }, [segments]);

  function toggle(key: string, keys: string | string[]) {
    const next = Array.isArray(keys) ? keys.map(String) : [String(keys)];
    setActiveKeys((current) => current.includes(key) ? current.filter((item) => item !== key) : [...new Set([...current, ...next])]);
  }

  function renderToolBatch(segment: RunPresentationSegment) {
    const tools = (segment.tools ?? []).filter((tool) => !isHiddenRecoverableToolFailure(tool));
    if (!tools.length) return null;
    if (tools.length === 1) {
      const tool = tools[0];
      const label = isUserDenied(tool) ? `${tool.name} · 已拒绝` : `调用 ${tool.name}`;
      return (
        <Collapse
          key={segment.segment_id}
          className="runtime-collapse runtime-tool-batch"
          ghost
          size="small"
          activeKey={activeKeys.includes(segment.segment_id) ? [segment.segment_id] : []}
          onChange={(keys) => toggle(segment.segment_id, keys)}
          items={[{ key: segment.segment_id, label, children: presentationToolBody(tool, display) }]}
        />
      );
    }
    return (
      <Collapse
        key={segment.segment_id}
        className="runtime-collapse runtime-tool-batch"
        ghost
        size="small"
        activeKey={activeKeys.includes(segment.segment_id) ? [segment.segment_id] : []}
        onChange={(keys) => toggle(segment.segment_id, keys)}
        items={[{
          key: segment.segment_id,
          label: "并行工具调用",
          children: <Collapse className="runtime-collapse" ghost size="small" items={tools.map((tool) => ({ key: tool.call_id, label: `${tool.name} · ${isUserDenied(tool) ? "已拒绝" : tool.status}`, children: presentationToolBody(tool, display) }))} />,
        }]}
      />
    );
  }

  return (
    <div className="runtime-segments">
      {segments.map((segment) => {
        if (segment.segment_type === "response") {
          return segment.text ? <div className="runtime-segment-response" key={segment.segment_id}><MarkdownContent text={segment.text} /></div> : null;
        }
        if (segment.segment_type === "tool_batch") return renderToolBatch(segment);
        const thought = display === "minimal" ? summarizeThinking(segment.text ?? "") : segment.text ?? "";
        return (
          <Collapse
            key={segment.segment_id}
            className="runtime-collapse runtime-thinking-segment"
            ghost
            size="small"
            activeKey={activeKeys.includes(segment.segment_id) ? [segment.segment_id] : []}
            onChange={(keys) => toggle(segment.segment_id, keys)}
            items={[{ key: segment.segment_id, label: "思考", children: display === "minimal" ? <ShimmerText active={segment.status === "streaming"}>{thought || "正在思考…"}</ShimmerText> : <MarkdownContent text={thought || "正在思考…"} /> }]}
          />
        );
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
  const frame = (
    <div className={msg.running ? "assistant-run-frame is-running" : "assistant-run-frame"}>
      {msg.segments?.length ? <RunSegments msg={msg} configuredDisplay={display} /> : <RuntimeDetails msg={msg} configuredDisplay={display} />}
      {msg.decision ? <DecisionCard request={msg.decision} onSubmit={(choice, options) => onDecision(msg.decision!, choice, options)} /> : null}
      {msg.error ? <Alert className="error-text" type="error" showIcon title={`⚠️ ${msg.error}`} /> : null}
      {!msg.segments?.length && msg.content ? <MarkdownContent text={msg.content} /> : null}
      {!msg.error && !msg.content && msg.running && !msg.decision ? <div className="thinking" role="status" aria-label="思考中" data-state="thinking" aria-live="polite"><span className="dot" /><span className="dot" /><span className="dot" /></div> : null}
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
