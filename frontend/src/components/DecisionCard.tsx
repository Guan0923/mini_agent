import { useState } from "react";
import type { DecisionRequest } from "../types";
import MarkdownContent from "./MarkdownContent";

interface Props {
  request: DecisionRequest;
  onSubmit: (choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
}

export default function DecisionCard({ request, onSubmit }: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [supplement, setSupplement] = useState("");
  const [answers, setAnswers] = useState<Record<string, string[]>>({});

  async function submit(choice: string, options: { supplement?: string; answers?: Record<string, string[]> } = {}) {
    setSubmitting(true);
    try {
      await onSubmit(choice, options);
    } finally {
      setSubmitting(false);
    }
  }

  if (request.kind === "plan") {
    const proposal = request.plan || (request.steps ?? []).map((step, index) => `${index + 1}. ${step}`).join("\n");
    return (
      <div className="decision-card plan-decision">
        <div className="decision-title">📋 Plan Review</div>
        {proposal ? <MarkdownContent text={proposal} /> : <p>{request.message || "Agent 请求审核一个计划。"}</p>}
        <div className="decision-actions">
          <button disabled={submitting} onClick={() => void submit("implement")}>实施</button>
          <button disabled={submitting} onClick={() => void submit("implement_clear_session")}>实施并清空会话</button>
          <button className="secondary" disabled={submitting} onClick={() => void submit("cancel")}>取消并留在 Plan</button>
        </div>
      </div>
    );
  }

  if (request.kind === "question") {
    const questions = request.questions ?? [];
    return (
      <div className="decision-card question-decision">
        <div className="decision-title">❓ Agent 需要你的回答</div>
        {questions.map((question) => (
          <div className="question-block" key={question.id}>
            <strong>{question.header || "问题"}</strong>
            <p>{question.question}</p>
            <div className="question-options">
              {question.options.map((option) => (
                <button
                  className={answers[question.id]?.[0] === option.label ? "selected" : ""}
                  disabled={submitting}
                  key={option.label}
                  onClick={() => setAnswers((current) => ({ ...current, [question.id]: [option.label] }))}
                >
                  {option.label}
                  {option.description ? <small>{option.description}</small> : null}
                </button>
              ))}
              <input
                placeholder="其他回答（可选）"
                value={
                  answers[question.id]?.[0] && !question.options.some((option) => option.label === answers[question.id]?.[0])
                    ? answers[question.id][0]
                    : ""
                }
                onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: [event.target.value] }))}
              />
            </div>
          </div>
        ))}
        <div className="decision-actions">
          <button
            disabled={submitting || questions.some((question) => !(answers[question.id]?.[0] || "").trim())}
            onClick={() => void submit("answer", { answers })}
          >
            提交回答
          </button>
        </div>
      </div>
    );
  }

  if (request.kind === "resume") {
    return (
      <div className="decision-card">
        <div className="decision-title">↻ 恢复运行</div>
        {request.details ? <pre>{request.details}</pre> : <p>{request.message || "是否继续这个持久运行？"}</p>}
        <div className="decision-actions">
          <button disabled={submitting} onClick={() => void submit("continue")}>继续</button>
          <button className="secondary" disabled={submitting} onClick={() => void submit("back")}>返回</button>
        </div>
      </div>
    );
  }

  const shownArguments =
    typeof request.arguments === "string" ? request.arguments : JSON.stringify(request.arguments ?? {}, null, 2);
  return (
    <div className="decision-card tool-decision">
      <div className="decision-title">🔐 工具审批</div>
      <p>{request.message || `请求调用 ${request.tool || "工具"}`}</p>
      {request.tool ? <strong className="mono">{request.tool}</strong> : null}
      <pre>{shownArguments}</pre>
      <input
        placeholder="补充说明（可选）"
        value={supplement}
        onChange={(event) => setSupplement(event.target.value)}
        disabled={submitting}
      />
      <div className="decision-actions">
        <button disabled={submitting} onClick={() => void submit("continue")}>继续</button>
        <button
          disabled={submitting || !supplement.trim()}
          onClick={() => void submit("supplement", { supplement: supplement.trim() })}
        >
          提交补充
        </button>
        <button className="secondary" disabled={submitting} onClick={() => void submit("cancel")}>取消</button>
      </div>
    </div>
  );
}
