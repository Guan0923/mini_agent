import { useEffect, useState } from "react";
import { Alert, Button, Card, Col, Collapse, Row, Spin, Statistic, Tag, Typography } from "antd";
import {
  ApiOutlined,
  BarChartOutlined,
  ClockCircleOutlined,
  PlayCircleOutlined,
  TeamOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { listTasks, runAllBenchmark, runBenchmark } from "../api";
import type { BenchmarkResult, BenchmarkTraceEvent, TaskInfo } from "../types";

interface RunState {
  busy: boolean;
  result: BenchmarkResult | null;
  error: string | null;
}

const CAPABILITY_LABEL: Record<string, string> = {
  terminal: "终端任务",
  software_engineering: "软件修复",
  tool_workflow: "工具工作流",
};

function scoreColor(score: number | null | undefined): string {
  if (score == null) return "#999";
  if (score >= 0.9) return "#16a34a";
  if (score >= 0.5) return "#d97706";
  return "#dc2626";
}

function ResultCard({ result }: { result: BenchmarkResult }) {
  const metrics = (result.metrics ?? {}) as Record<string, unknown>;
  const score = (result.score as number | null | undefined) ?? null;
  const verdicts = (result.verdicts ?? []) as Array<Record<string, unknown>>;
  const passed = result.passed;
  const statusLabel = passed === true ? "通过" : passed === false ? "未通过" : String(result.status ?? "?");
  const statusColor = passed === true ? "success" : passed === false ? "error" : "processing";
  const trace = Array.isArray(result.trace) ? result.trace as BenchmarkTraceEvent[] : [];
  const failurePhase = typeof result.failure_phase === "string" ? result.failure_phase : "";

  function jsonText(value: unknown): string {
    try {
      return JSON.stringify(value ?? {}, null, 2);
    } catch {
      return String(value);
    }
  }

  return (
    <Card className="result-card" size="small">
      <div className="result-top">
        <Statistic
          className="score"
          title="得分"
          value={score != null ? score * 100 : "未评分"}
          precision={score != null ? 0 : undefined}
          suffix={score != null ? "分" : undefined}
          styles={{ content: { color: scoreColor(score) } }}
        />
        <Tag color={statusColor}>状态：{statusLabel}</Tag>
        {result.error ? <Alert className="error-text" title={String(result.error)} type="error" showIcon /> : null}
        {failurePhase ? <Typography.Text type="secondary">失败阶段：{failurePhase}</Typography.Text> : null}
      </div>
      <Row className="result-metrics" gutter={[12, 12]}>
        <Col xs={12} sm={8}><Statistic prefix={<ClockCircleOutlined />} title="耗时" value={Number(metrics.duration_ms ?? 0) / 1000} precision={1} suffix="s" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<ApiOutlined />} title="模型调用" value={Number(metrics.model_calls ?? 0)} suffix="次" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<ToolOutlined />} title="工具调用" value={Number(metrics.tool_calls ?? 0)} suffix="次" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<TeamOutlined />} title="子代理完成" value={Number(metrics.subagent_completed ?? 0)} suffix="个" /></Col>
        <Col xs={12} sm={8}><Statistic prefix={<BarChartOutlined />} title="Tokens" value={Number(metrics.total_tokens ?? 0)} /></Col>
      </Row>
      {verdicts.length > 0 ? (
        <ul className="verdicts">
          {verdicts.map((verdict, index) => {
            const ok = Number(verdict.score) >= 1;
            return (
              <li key={index} className={ok ? "ok" : "no"}>
                <Tag color={ok ? "success" : "error"}>{ok ? "✓" : "✗"}</Tag> {String(verdict.detail ?? "")}
              </li>
            );
          })}
        </ul>
      ) : null}
      {result.final_answer ? (
        <Collapse
          className="final-answer"
          size="small"
          items={[{ key: "answer", label: "最终答复", children: <pre>{String(result.final_answer)}</pre> }]}
        />
      ) : null}
      <Collapse
        className="benchmark-trace"
        size="small"
        items={[{
          key: "trace",
          label: `完整 Trace（${trace.length} 条事件）`,
          children: trace.length === 0 ? <Typography.Text type="secondary">没有可显示的运行事件。</Typography.Text> : (
            <div className="benchmark-trace-list">
              {trace.map((event, index) => (
                <div className="benchmark-trace-event" key={`${event.timestamp}-${event.kind}-${index}`}>
                  <div className="benchmark-trace-head">
                    <Tag>{event.kind}</Tag>
                    <Typography.Text type="secondary">{event.timestamp}</Typography.Text>
                  </div>
                  {event.message ? <pre className="benchmark-trace-message">{event.message}</pre> : null}
                  <pre className="benchmark-trace-data">{jsonText(event.data)}</pre>
                </div>
              ))}
            </div>
          ),
        }]}
      />
    </Card>
  );
}

export default function BenchmarkPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const planner = "llm" as const;
  const [runs, setRuns] = useState<Record<string, RunState>>({});
  const [allBusy, setAllBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    setTasksLoading(true);
    listTasks()
      .then(setTasks)
      .catch((e) => setLoadError(String(e?.message ?? e)))
      .finally(() => setTasksLoading(false));
  }, []);

  function runTask(name: string, p: string) {
    setLoadError(null);
    setRuns((prev) => ({ ...prev, [name]: { busy: true, result: null, error: null } }));
    runBenchmark(name, p)
      .then((result) => setRuns((prev) => ({ ...prev, [name]: { busy: false, result, error: null } })))
      .catch((e) =>
        setRuns((prev) => ({ ...prev, [name]: { busy: false, result: null, error: String(e?.message ?? e) } })),
      );
  }

  function runAll() {
    if (allBusy) return;
    setLoadError(null);
    setAllBusy(true);
    runAllBenchmark(planner)
      .then((results) => {
        const map: Record<string, RunState> = {};
        for (const result of results) {
          map[String(result.task_name)] = { busy: false, result, error: null };
        }
        setRuns((prev) => ({ ...prev, ...map }));
      })
      .catch((e) => setLoadError(String(e?.message ?? e)))
      .finally(() => setAllBusy(false));
  }

  return (
    <div className="benchmark-page">
      <Card className="page-header" variant="borderless">
        <h1>Benchmark 成绩单</h1>
        <p>让 agent 完成一批开源来源适配任务并自动判卷；成绩只代表 Mini-Agent adapted suite</p>
        <div className="bench-toolbar">
          <div className="planner-select">
            <label>运行方式</label>
            <span className="muted">真实模型（llm）；无 rule 冒烟题</span>
          </div>
          <Button className="run-all" type="primary" icon={<PlayCircleOutlined />} onClick={runAll} loading={allBusy}>
            全部运行
          </Button>
        </div>
      </Card>

      {loadError ? <Alert className="error-text" title={loadError} type="error" showIcon /> : null}

      {tasksLoading ? (
        <div className="benchmark-loading"><Spin description="正在加载基准任务…" /></div>
      ) : tasks.length === 0 ? (
        <Alert type="info" showIcon title="暂无可运行的基准任务。" />
      ) : (
        <Row className="task-grid" gutter={[16, 16]}>
          {tasks.map((task) => {
            const run = runs[task.name];
            return (
              <Col xs={24} lg={12} key={task.name}>
                <Card className="task-card">
                  <div className="task-head">
                    <span className="task-name">{task.name}</span>
                    <span className="task-badges">
                      <Tag className="capability-badge" color="blue">{CAPABILITY_LABEL[task.capability] ?? task.capability}</Tag>
                      <Tag className="source-badge">{task.source.benchmark}</Tag>
                    </span>
                  </div>
                  <p className="task-desc">
                    {task.description} · 难度：{task.difficulty} · 来源：
                    <a href={task.source.url} target="_blank" rel="noreferrer">
                      {task.source.benchmark} / {task.source.task_id}
                    </a>
                  </p>
                  <Collapse
                    className="task-source"
                    size="small"
                    items={[
                      {
                        key: "source",
                        label: "适配说明与许可证",
                        children: <><p>{task.source.adaptation_notes}</p><p>{task.source.license} · {task.source.source_revision}</p></>,
                      },
                    ]}
                  />
                  <Collapse
                    className="task-details"
                    size="small"
                    items={[{
                      key: "details",
                      label: "完整测试内容",
                      children: (
                        <>
                          <Typography.Paragraph>{task.description}</Typography.Paragraph>
                          <Typography.Text strong>测试 Prompt</Typography.Text>
                          <pre className="benchmark-task-prompt">{task.prompt}</pre>
                          <Typography.Text type="secondary">
                            预算：工具调用 {task.budgets.max_tool_calls}
                          </Typography.Text>
                          {task.tags.length > 0 ? <div className="benchmark-task-tags">{task.tags.map((tag) => <Tag key={tag}>{tag}</Tag>)}</div> : null}
                        </>
                      ),
                    }]}
                  />
                  <div className="task-actions">
                    {task.planner_modes.includes(planner) ? (
                      <Button className="send-btn" type="primary" icon={<PlayCircleOutlined />} onClick={() => runTask(task.name, planner)} loading={run?.busy}>
                        运行
                      </Button>
                    ) : (
                      <span className="muted">该任务不支持 {planner} 模式</span>
                    )}
                  </div>
                  {run?.error ? <Alert className="error-text" title={run.error} type="error" showIcon /> : null}
                  {run?.result ? <ResultCard result={run.result} /> : null}
                </Card>
              </Col>
            );
          })}
        </Row>
      )}
    </div>
  );
}
