import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Alert, Button, Collapse, Empty, Select, Spin, Tag, type CollapseProps } from "antd";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { getTurnTrace } from "../../api";
import type {
  RuntimeStateNode,
  TurnItem,
  TurnMessage,
  TurnTraceRequest,
  TurnTraceResponse,
} from "../../types";

interface TracePageProps {
  turns: RuntimeStateNode[];
}

type SemanticKind = "system" | "preference" | "skill" | "mcp" | "user" | "reasoning" | "assistant" | "tool";

const TRACE_TAGS: Record<SemanticKind, { label: string; color?: string }> = {
  system: { label: "System", color: "purple" },
  preference: { label: "Preference", color: "magenta" },
  skill: { label: "Skill", color: "cyan" },
  mcp: { label: "MCP", color: "orange" },
  user: { label: "User Message", color: "green" },
  reasoning: { label: "Assistant Reasoning", color: "gold" },
  assistant: { label: "Assistant Response", color: "blue" },
  tool: { label: "Tool" },
};

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function text(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return json(value);
}

function preview(value: unknown): string {
  return text(value).replace(/\s+/g, " ").trim() || "（空）";
}

function TraceLabel({ kind, value, status, timestamp }: {
  kind: SemanticKind;
  value: unknown;
  status?: string;
  timestamp?: string;
}) {
  const tag = TRACE_TAGS[kind];
  const failed = status === "failed";
  return (
    <span className="trace-collapse-label">
      <Tag color={failed ? "red" : tag.color}>{tag.label}</Tag>
      {status ? <Tag color={failed ? "red" : undefined}>{status}</Tag> : null}
      {timestamp ? <time>{timestamp}</time> : null}
      <span className="trace-preview" title={preview(value)}>{preview(value)}</span>
    </span>
  );
}

function panel(
  key: string,
  kind: SemanticKind,
  value: unknown,
  options: { status?: string; timestamp?: string; body?: ReactNode } = {},
): NonNullable<CollapseProps["items"]>[number] {
  return {
    key,
    label: <TraceLabel kind={kind} value={value} status={options.status} timestamp={options.timestamp} />,
    children: options.body ?? <pre className="trace-value">{text(value)}</pre>,
  };
}

function requestPanels(request: TurnTraceRequest): NonNullable<CollapseProps["items"]> {
  const result: NonNullable<CollapseProps["items"]> = [];
  result.push(panel(`${request.exchange_id}:meta`, "tool", request.operation, {
    timestamp: request.timestamp,
    body: <pre className="trace-value">{json({
      exchange_id: request.exchange_id,
      sequence: request.sequence,
      provider: request.provider,
      provider_name: request.provider_name,
      model: request.model,
      operation: request.operation,
      output_mode: request.output_mode,
      stream: request.stream,
      request_parameters: request.request_parameters,
    })}</pre>,
  }));
  result.push(panel(`${request.exchange_id}:base-system`, "system", request.base_system_prompt));
  result.push(panel(`${request.exchange_id}:effective-system`, "system", request.effective_system_prompt));
  if (request.user_preferences) {
    result.push(panel(`${request.exchange_id}:preferences`, "preference", request.user_preferences));
  }
  request.skills.forEach((skill, index) => {
    result.push(panel(`${request.exchange_id}:skill:${index}`, "skill", skill.instructions ?? skill, {
      body: <pre className="trace-value">{json(skill)}</pre>,
    }));
  });
  request.tools.forEach((tool, index) => {
    const kind = tool.origin?.kind === "mcp" ? "mcp" : "tool";
    result.push(panel(`${request.exchange_id}:tool:${index}`, kind, tool.name, {
      body: <pre className="trace-value">{json(tool)}</pre>,
    }));
  });
  request.messages.forEach((message, index) => {
    const role = String(message.role ?? "");
    if (role === "system") return;
    if (role === "user") {
      result.push(panel(`${request.exchange_id}:message:${index}`, "user", message.content));
      return;
    }
    if (message.reasoning) {
      result.push(panel(`${request.exchange_id}:reasoning:${index}`, "reasoning", message.reasoning));
    }
    if (message.content) {
      result.push(panel(`${request.exchange_id}:message:${index}`, "assistant", message.content));
    }
    if (Array.isArray(message.tool_messages)) {
      message.tool_messages.forEach((tool, toolIndex) => {
        result.push(panel(`${request.exchange_id}:message:${index}:tool:${toolIndex}`, "tool", tool, {
          body: <pre className="trace-value">{json(tool)}</pre>,
        }));
      });
    }
  });
  return result;
}

function itemValue(item: TurnItem): unknown {
  return item.text ?? item.message ?? item.content ?? item.summary ?? item;
}

function turnPanels(messages: TurnMessage[]): NonNullable<CollapseProps["items"]> {
  const result: NonNullable<CollapseProps["items"]> = [];
  messages.forEach((message, messageIndex) => {
    message.content.forEach((item, itemIndex) => {
      const key = `turn:${messageIndex}:${itemIndex}`;
      if (message.role === "user") {
        result.push(panel(key, "user", itemValue(item), { status: item.status }));
        return;
      }
      const kind: SemanticKind = item.type === "reasoning"
        ? "reasoning"
        : item.type === "text"
          ? "assistant"
          : "tool";
      result.push(panel(key, kind, itemValue(item), {
        status: item.status,
        body: <pre className="trace-value">{typeof itemValue(item) === "string" ? text(itemValue(item)) : json(item)}</pre>,
      }));
    });
  });
  return result;
}

export default function TracePage({ turns }: TracePageProps) {
  const orderedTurns = useMemo(
    () => [...turns].sort((left, right) => right.timestamp.localeCompare(left.timestamp) || right.id.localeCompare(left.id)),
    [turns],
  );
  const [requestedTurnId, setRequestedTurnId] = useState<string | null>(null);
  const selectedTurn = orderedTurns.find((turn) => turn.id === requestedTurnId) ?? orderedTurns[0];
  const [versionByTurn, setVersionByTurn] = useState<Record<string, number>>({});
  const requestedDataIdx = selectedTurn
    ? versionByTurn[selectedTurn.id] ?? selectedTurn.current_data_idx
    : 0;
  const dataIdx = selectedTurn ? Math.min(Math.max(requestedDataIdx, 0), selectedTurn.data.length - 1) : 0;
  const [trace, setTrace] = useState<TurnTraceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const traceStatus = trace?.turn.id === selectedTurn?.id ? trace.turn.status : selectedTurn?.status;

  useEffect(() => {
    if (!selectedTurn) {
      setTrace(null);
      return;
    }
    const controller = new AbortController();
    let mounted = true;
    const load = async (showLoading: boolean) => {
      if (showLoading) setLoading(true);
      try {
        const value = await getTurnTrace(selectedTurn.id, dataIdx, controller.signal);
        if (mounted) {
          setTrace(value);
          setError(null);
        }
      } catch (reason) {
        if (mounted && !controller.signal.aborted) setError(String((reason as Error).message ?? reason));
      } finally {
        if (mounted && showLoading) setLoading(false);
      }
    };
    void load(true);
    const interval = traceStatus === "running" ? window.setInterval(() => void load(false), 2_000) : undefined;
    return () => {
      mounted = false;
      controller.abort();
      if (interval !== undefined) window.clearInterval(interval);
    };
  }, [selectedTurn?.id, dataIdx, traceStatus]);

  if (!selectedTurn) return <div className="trace-page"><Empty description="当前 Thread 还没有 Turn" /></div>;

  const response = trace?.turn.id === selectedTurn.id && trace.data_idx === dataIdx ? trace : null;
  const selectedMessages = response?.turn.data[dataIdx] ?? selectedTurn.data[dataIdx] ?? [];
  const innerItems = [
    ...(response?.requests.flatMap(requestPanels) ?? []),
    ...turnPanels(selectedMessages),
  ];
  const outerItems: CollapseProps["items"] = [{
    key: selectedTurn.id,
    label: (
      <span className="trace-turn-label">
        <Tag color={selectedTurn.status === "failed" ? "red" : "blue"}>Turn</Tag>
        <span>{selectedTurn.id}</span>
        <Tag color={selectedTurn.status === "failed" ? "red" : undefined}>{selectedTurn.status}</Tag>
        <time>{selectedTurn.timestamp}</time>
      </span>
    ),
    children: innerItems.length > 0
      ? <Collapse className="trace-inner-collapse" items={innerItems} />
      : <Empty description="该版本没有审计快照或 Item" />,
  }];

  return (
    <div className="trace-page">
      <div className="trace-controls">
        <Select
          aria-label="选择 Turn"
          variant="borderless"
          value={selectedTurn.id}
          options={orderedTurns.map((turn) => ({ label: `${turn.id} · ${turn.status}`, value: turn.id }))}
          onChange={(turnId) => setRequestedTurnId(turnId)}
        />
        <span className="trace-version-controls">
          <Button
            type="text"
            aria-label="上一个 data 版本"
            icon={<LeftOutlined />}
            disabled={dataIdx <= 0}
            onClick={() => setVersionByTurn((current) => ({ ...current, [selectedTurn.id]: dataIdx - 1 }))}
          />
          <span>data {dataIdx + 1}/{selectedTurn.data.length}</span>
          <Button
            type="text"
            aria-label="下一个 data 版本"
            icon={<RightOutlined />}
            disabled={dataIdx >= selectedTurn.data.length - 1}
            onClick={() => setVersionByTurn((current) => ({ ...current, [selectedTurn.id]: dataIdx + 1 }))}
          />
        </span>
      </div>
      {error ? <Alert type="error" showIcon title={`Trace 加载失败：${error}`} /> : null}
      {loading && !response ? <div className="trace-loading"><Spin /></div> : null}
      <Collapse key={selectedTurn.id} className="trace-turn-collapse" defaultActiveKey={[selectedTurn.id]} items={outerItems} />
    </div>
  );
}
