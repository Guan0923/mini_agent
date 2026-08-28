import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Alert, Button, Collapse, Empty, Select, Spin, Tag, type CollapseProps } from "antd";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { getTurnTrace } from "../../api";
import type {
  RuntimeStateNode,
  TurnItem,
  TurnTraceContext,
  TurnTraceItem,
  TurnTraceResponse,
} from "../../types";

interface TracePageProps {
  turns: RuntimeStateNode[];
}

type SemanticKind = "system" | "skill" | "mcp" | "user" | "reasoning" | "assistant" | "tool";

const TRACE_TAGS: Record<SemanticKind, { label: string; color?: string }> = {
  system: { label: "System", color: "purple" },
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

function contextPanels(context: TurnTraceContext | null): NonNullable<CollapseProps["items"]> {
  if (!context) return [];
  const result: NonNullable<CollapseProps["items"]> = [
    panel("context:system", "system", context.system_message, { timestamp: context.initialized_at }),
  ];
  context.active_skills.forEach((skill, index) => {
    result.push(panel(`context:skill:${index}`, "skill", skill.instructions ?? skill, {
      body: <pre className="trace-value">{json(skill)}</pre>,
    }));
  });
  context.tools.forEach((tool, index) => {
    const kind = tool.origin?.kind === "mcp" ? "mcp" : "tool";
    result.push(panel(`context:tool:${index}`, kind, tool.name, {
      body: <pre className="trace-value">{json(tool)}</pre>,
    }));
  });
  return result;
}

function itemValue(item: TurnItem): unknown {
  return item.text ?? item.message ?? item.content ?? item.summary ?? item;
}

function itemKind(entry: TurnTraceItem): SemanticKind {
  if (entry.role === "user") return "user";
  if (entry.item.type === "reasoning") return "reasoning";
  if (entry.item.type === "text") return "assistant";
  if (entry.item.type === "skill_snapshot") return "skill";
  return "tool";
}

function traceItemPanels(items: TurnTraceItem[]): NonNullable<CollapseProps["items"]> {
  return items.map((entry) => {
    const value = itemValue(entry.item);
    return panel(
      `item:${entry.message_idx}:${entry.item_idx}`,
      itemKind(entry),
      value,
      {
        status: entry.item.status,
        timestamp: entry.completed_at,
        body: (
          <pre className="trace-value">
            {typeof value === "string" && entry.item.type !== "tool_call" ? text(value) : json(entry.item)}
          </pre>
        ),
      },
    );
  });
}

function mergeTrace(current: TurnTraceResponse | null, incoming: TurnTraceResponse): TurnTraceResponse {
  if (!current || current.turn.id !== incoming.turn.id || current.data_idx !== incoming.data_idx) return incoming;
  const sequences = new Set(current.items.map((item) => item.sequence));
  return {
    ...incoming,
    context: current.context ?? incoming.context,
    items: [...current.items, ...incoming.items.filter((item) => !sequences.has(item.sequence))]
      .sort((left, right) => left.sequence - right.sequence),
  };
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

  useEffect(() => {
    if (!selectedTurn) {
      setTrace(null);
      return;
    }
    let stopped = false;
    let activeController: AbortController | null = null;
    let timer: number | undefined;
    let cursor = 0;
    let hasContext = false;
    let running = selectedTurn.status === "running";
    setTrace(null);
    setError(null);

    const schedule = () => {
      if (!stopped && running) timer = window.setTimeout(() => void load(false), 2_000);
    };
    const load = async (showLoading: boolean) => {
      if (showLoading) setLoading(true);
      activeController = new AbortController();
      try {
        const value = await getTurnTrace(
          selectedTurn.id,
          dataIdx,
          activeController.signal,
          hasContext ? cursor : undefined,
        );
        if (stopped) return;
        hasContext ||= value.context !== null;
        cursor = Math.max(cursor, value.last_sequence);
        running = value.turn.status === "running";
        setTrace((current) => mergeTrace(current, value));
        setError(null);
        schedule();
      } catch (reason) {
        if (!stopped && !activeController.signal.aborted) {
          setError(String((reason as Error).message ?? reason));
          schedule();
        }
      } finally {
        if (!stopped && showLoading) setLoading(false);
      }
    };
    void load(true);
    return () => {
      stopped = true;
      activeController?.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [selectedTurn?.id, dataIdx]);

  if (!selectedTurn) return <div className="trace-page"><Empty description="当前 Thread 还没有 Turn" /></div>;

  const response = trace?.turn.id === selectedTurn.id && trace.data_idx === dataIdx ? trace : null;
  const displayTurn = response?.turn ?? selectedTurn;
  const innerItems = [
    ...contextPanels(response?.context ?? null),
    ...traceItemPanels(response?.items ?? []),
  ];
  const outerItems: CollapseProps["items"] = [{
    key: selectedTurn.id,
    label: (
      <span className="trace-turn-label">
        <Tag color={displayTurn.status === "failed" ? "red" : "blue"}>Turn</Tag>
        <span className="trace-turn-id" title={displayTurn.id}>{displayTurn.id}</span>
        <Tag color={displayTurn.status === "failed" ? "red" : undefined}>{displayTurn.status}</Tag>
        <time>{displayTurn.timestamp}</time>
      </span>
    ),
    children: innerItems.length > 0
      ? <Collapse
          className="trace-inner-collapse"
          classNames={{ title: "trace-collapse-title" }}
          items={innerItems}
        />
      : <Empty description="该版本还没有 Trace 上下文或完成 Item" />,
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
      <Collapse
        key={`${selectedTurn.id}:${dataIdx}`}
        className="trace-turn-collapse"
        classNames={{ title: "trace-collapse-title" }}
        defaultActiveKey={[selectedTurn.id]}
        items={outerItems}
      />
    </div>
  );
}
