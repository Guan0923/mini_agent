import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Alert, Button, Collapse, Empty, Spin, Tag, type CollapseProps } from "antd";
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

function TurnTraceContent({ turn, dataIdx, active }: {
  turn: RuntimeStateNode;
  dataIdx: number;
  active: boolean;
}) {
  const [trace, setTrace] = useState<TurnTraceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!active) {
      setTrace(null);
      setLoading(false);
      setError(null);
      return;
    }
    let stopped = false;
    let activeController: AbortController | null = null;
    let timer: number | undefined;
    let cursor = 0;
    let hasContext = false;
    let running = turn.status === "running";
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
          turn.id,
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
  }, [active, turn.id, dataIdx]);

  const response = trace?.turn.id === turn.id && trace.data_idx === dataIdx ? trace : null;
  const innerItems = [
    ...contextPanels(response?.context ?? null),
    ...traceItemPanels(response?.items ?? []),
  ];

  return (
    <div className="trace-turn-content">
      {error ? <Alert type="error" showIcon title={`Trace 加载失败：${error}`} /> : null}
      {loading && !response ? <div className="trace-loading"><Spin /></div> : null}
      {response && innerItems.length > 0
        ? <Collapse
            className="trace-inner-collapse"
            classNames={{ title: "trace-collapse-title" }}
            items={innerItems}
          />
        : null}
      {!loading && !error && innerItems.length === 0
        ? <Empty description="该版本还没有 Trace 上下文或完成 Item" />
        : null}
    </div>
  );
}

function clampDataIdx(turn: RuntimeStateNode, requestedDataIdx: number): number {
  return Math.min(Math.max(requestedDataIdx, 0), Math.max(turn.data.length - 1, 0));
}

export default function TracePage({ turns }: TracePageProps) {
  const orderedTurns = useMemo(
    () => [...turns].sort((left, right) => left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id)),
    [turns],
  );
  const [activeTurnIds, setActiveTurnIds] = useState<string[]>(() => {
    const latestTurn = orderedTurns[orderedTurns.length - 1];
    return latestTurn ? [latestTurn.id] : [];
  });
  const activeTurnIdSet = useMemo(() => new Set(activeTurnIds), [activeTurnIds]);
  const [versionByTurn, setVersionByTurn] = useState<Record<string, number>>({});

  if (orderedTurns.length === 0) {
    return <div className="trace-page"><Empty description="当前 Thread 还没有 Turn" /></div>;
  }

  const outerItems: CollapseProps["items"] = orderedTurns.map((turn) => {
    const dataIdx = clampDataIdx(turn, versionByTurn[turn.id] ?? turn.current_data_idx);
    const totalVersions = turn.data.length;
    const versionControls = (
      <span className="trace-version-controls" onClick={(event) => event.stopPropagation()}>
        <Button
          type="text"
          aria-label={`${turn.id} 上一个 data 版本`}
          icon={<LeftOutlined />}
          disabled={dataIdx <= 0}
          onClick={() => setVersionByTurn((current) => ({ ...current, [turn.id]: dataIdx - 1 }))}
        />
        <span className="trace-version-label">
          <span className="trace-version-prefix">data </span>{dataIdx + 1}/{totalVersions}
        </span>
        <Button
          type="text"
          aria-label={`${turn.id} 下一个 data 版本`}
          icon={<RightOutlined />}
          disabled={dataIdx >= totalVersions - 1}
          onClick={() => setVersionByTurn((current) => ({ ...current, [turn.id]: dataIdx + 1 }))}
        />
      </span>
    );

    return {
      key: turn.id,
      label: (
        <span className="trace-turn-label">
          <Tag color={turn.status === "failed" ? "red" : "blue"}>Turn</Tag>
          <span className="trace-turn-id" title={turn.id}>{turn.id}</span>
          <Tag color={turn.status === "failed" ? "red" : undefined}>{turn.status}</Tag>
          <time>{turn.timestamp}</time>
        </span>
      ),
      extra: versionControls,
      children: (
        <TurnTraceContent
          turn={turn}
          dataIdx={dataIdx}
          active={activeTurnIdSet.has(turn.id)}
        />
      ),
    };
  });

  return (
    <div className="trace-page">
      <Collapse
        className="trace-turn-collapse"
        classNames={{ title: "trace-collapse-title" }}
        activeKey={activeTurnIds}
        onChange={(keys) => setActiveTurnIds(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
        items={outerItems}
      />
    </div>
  );
}
