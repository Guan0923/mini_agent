import { useEffect, useState } from "react";
import { listTasks, runAllBenchmark, runBenchmark } from "../api";
import type { TaskInfo } from "../types";

interface RunState {
  busy: boolean;
  result: Record<string, unknown> | null;
  error: string | null;
}

const CAPABILITY_LABEL: Record<string, string> = {
  tools: "工具",
  skills: "技能",
  mcp: "MCP",
  subagents: "子代理",
};

function scoreColor(score: number | null | undefined): string {
  if (score == null) return "#999";
  if (score >= 0.9) return "#16a34a";
  if (score >= 0.5) return "#d97706";
  return "#dc2626";
}

function ResultCard({ result }: { result: Record<string, unknown> }) {
  const metrics = (result.metrics ?? {}) as Record<string, unknown>;
  const score = result.score as number | null;
  const verdicts = (result.verdicts ?? []) as Array<Record<string, unknown>>;
  return (
    <div className="result-card">
      <div className="result-top">
        <span className="score" style={{ color: scoreColor(score) }}>
          {score != null ? (score * 100).toFixed(0) + "分" : "未评分"}
        </span>
        <span className="result-status">
          状态：{String(result.status ?? "?")}
          {result.error ? <span className="error-text"> · {String(result.error)}</span> : null}
        </span>
      </div>
      <div className="result-metrics">
        <span>⏱ {(Number(metrics.duration_ms) / 1000).toFixed(1)}s</span>
        <span>🔄 {String(metrics.model_calls ?? 0)} 次模型</span>
        <span>🔧 {String(metrics.tool_calls ?? 0)} 次工具</span>
        <span>👥 {String(metrics.subagent_completed ?? 0)} 个子代理完成</span>
        <span>📊 {String(metrics.total_tokens ?? 0)} tokens</span>
      </div>
      {verdicts.length > 0 && (
        <ul className="verdicts">
          {verdicts.map((v, i) => (
            <li key={i} className={Number(v.score) >= 1 ? "ok" : "no"}>
              {Number(v.score) >= 1 ? "✓" : "✗"} {String(v.detail ?? "")}
            </li>
          ))}
        </ul>
      )}
      {result.final_answer ? (
        <details className="final-answer">
          <summary>最终答复</summary>
          <pre>{String(result.final_answer)}</pre>
        </details>
      ) : null}
    </div>
  );
}

export default function BenchmarkPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [planner, setPlanner] = useState<"llm" | "rule">("llm");
  const [runs, setRuns] = useState<Record<string, RunState>>({});
  const [allBusy, setAllBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    listTasks()
      .then(setTasks)
      .catch((e) => setLoadError(String(e?.message ?? e)));
  }, []);

  function runTask(name: string, p: string) {
    setRuns((prev) => ({ ...prev, [name]: { busy: true, result: null, error: null } }));
    runBenchmark(name, p)
      .then((result) => setRuns((prev) => ({ ...prev, [name]: { busy: false, result, error: null } })))
      .catch((e) =>
        setRuns((prev) => ({ ...prev, [name]: { busy: false, result: null, error: String(e?.message ?? e) } })),
      );
  }

  function runAll() {
    setAllBusy(true);
    runAllBenchmark(planner)
      .then((results) => {
        const map: Record<string, RunState> = {};
        for (const r of results) {
          map[String(r.task_name)] = { busy: false, result: r, error: null };
        }
        setRuns((prev) => ({ ...prev, ...map }));
      })
      .catch((e) => setLoadError(String(e?.message ?? e)))
      .finally(() => setAllBusy(false));
  }

  return (
    <div className="benchmark-page">
      <header className="page-header">
        <h1>Benchmark 成绩单</h1>
        <p>让 agent 完成 10 道任务并自动判卷，衡量它的工具 / 技能 / MCP / 子代理能力</p>
        <div className="bench-toolbar">
          <div className="planner-select">
            <label>运行方式</label>
            <select
              value={planner}
              onChange={(e) => setPlanner(e.target.value as "llm" | "rule")}
            >
              <option value="llm">真实模型（llm）</option>
              <option value="rule">离线规则（rule，免费）</option>
            </select>
          </div>
          <button className="new-chat-btn run-all" onClick={runAll} disabled={allBusy}>
            {allBusy ? "运行中…" : "全部运行"}
          </button>
        </div>
      </header>

      {loadError ? <div className="error-text">⚠️ {loadError}</div> : null}

      <div className="task-grid">
        {tasks.map((t) => {
          const run = runs[t.name];
          return (
            <div className="task-card" key={t.name}>
              <div className="task-head">
                <span className="task-name">{t.name}</span>
                <span className="capability-badge">{CAPABILITY_LABEL[t.capability] ?? t.capability}</span>
              </div>
              <p className="task-desc">{t.description}</p>
              <div className="task-actions">
                {t.planner_modes.includes(planner) ? (
                  <button
                    className="send-btn"
                    onClick={() => runTask(t.name, planner)}
                    disabled={run?.busy}
                  >
                    {run?.busy ? "运行中…" : "运行"}
                  </button>
                ) : (
                  <span className="muted">该任务不支持 {planner} 模式</span>
                )}
              </div>
              {run?.busy ? <div className="spinner" /> : null}
              {run?.error ? <div className="error-text">⚠️ {run.error}</div> : null}
              {run?.result ? <ResultCard result={run.result} /> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
