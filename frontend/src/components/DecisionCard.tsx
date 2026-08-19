import { useState } from "react";
import { Button, Card, Input, Radio, Space } from "antd";
import {
  FileTextOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import type { DecisionRequest } from "../types";
import MarkdownContent from "./MarkdownContent";

interface Props {
  request: DecisionRequest;
  onSubmit: (choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
}

/**
 * Renders the four decision protocols emitted by the runtime. The choice
 * strings intentionally stay identical to the backend contract; only the
 * presentation controls are provided by Ant Design.
 */
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
      <Card className="decision-card plan-decision" size="small" title={<><FileTextOutlined /> Plan Review</>}>
        {proposal ? <MarkdownContent text={proposal} /> : <p>{request.message || "Agent 请求审核一个计划。"}</p>}
        <Space className="decision-actions" wrap>
          <Button autoInsertSpace={false} type="primary" loading={submitting} disabled={submitting} onClick={() => void submit("implement")}>实施</Button>
          <Button autoInsertSpace={false} type="primary" loading={submitting} disabled={submitting} onClick={() => void submit("implement_clear_session")}>实施并清空会话</Button>
          <Button autoInsertSpace={false} loading={submitting} disabled={submitting} onClick={() => void submit("cancel")}>取消并留在 Plan</Button>
        </Space>
      </Card>
    );
  }

  if (request.kind === "question") {
    const questions = request.questions ?? [];
    return (
      <Card className="decision-card question-decision" size="small" title={<><QuestionCircleOutlined /> Agent 需要你的回答</>}>
        {questions.map((question) => {
          const answer = answers[question.id]?.[0] ?? "";
          const knownOption = question.options.some((option) => option.label === answer);
          return (
            <div className="question-block" key={question.id}>
              <strong>{question.header || "问题"}</strong>
              <p>{question.question}</p>
              <Radio.Group
                className="question-options"
                value={knownOption ? answer : undefined}
                onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: [event.target.value] }))}
              >
                <Space orientation="vertical" size={6}>
                  {question.options.map((option) => (
                    <Radio disabled={submitting} value={option.label} key={option.label}>
                      <span>{option.label}</span>
                      {option.description ? <small>{option.description}</small> : null}
                    </Radio>
                  ))}
                </Space>
              </Radio.Group>
              <Input
                className="question-other-input"
                placeholder="其他回答（可选）"
                value={knownOption ? "" : answer}
                onChange={(event) => setAnswers((current) => ({ ...current, [question.id]: [event.target.value] }))}
                disabled={submitting}
              />
            </div>
          );
        })}
        <Space className="decision-actions" wrap>
          <Button
            autoInsertSpace={false}
            type="primary"
            loading={submitting}
            disabled={submitting || questions.some((question) => !(answers[question.id]?.[0] || "").trim())}
            onClick={() => void submit("answer", { answers })}
          >
            提交回答
          </Button>
        </Space>
      </Card>
    );
  }

  if (request.kind === "resume") {
    return (
      <Card className="decision-card" size="small" title={<><ReloadOutlined /> 恢复运行</>}>
        {request.details ? <pre>{request.details}</pre> : <p>{request.message || "是否继续这个持久运行？"}</p>}
        <Space className="decision-actions" wrap>
          <Button autoInsertSpace={false} type="primary" loading={submitting} disabled={submitting} onClick={() => void submit("continue")}>继续</Button>
          <Button autoInsertSpace={false} loading={submitting} disabled={submitting} onClick={() => void submit("back")}>返回</Button>
        </Space>
      </Card>
    );
  }

  if (request.kind === "skill") {
    return (
      <Card className="decision-card skill-decision" size="small" title={<><SafetyCertificateOutlined /> 项目 Skill 信任审批</>}>
        <p>{request.message || "这个项目 Skill 尚未被信任。"}</p>
        <strong className="mono">{request.skill || "unknown-skill"}</strong>
        {request.description ? <p>{request.description}</p> : null}
        {request.path ? (
          <p className="muted">
            项目路径：<code>{request.path}</code>
          </p>
        ) : null}
        {request.tree_sha256 ? (
          <p className="muted">
            目录指纹（SHA-256）：<code>{request.tree_sha256}</code>
          </p>
        ) : null}
        <p className="muted">
          信任只针对当前目录内容。Skill 中的脚本、引用或资料发生变化后需要重新审批。
        </p>
        <Space className="decision-actions" wrap>
          <Button autoInsertSpace={false} type="primary" loading={submitting} disabled={submitting} onClick={() => void submit("trust")}>信任这个 Skill</Button>
          <Button autoInsertSpace={false} loading={submitting} disabled={submitting} onClick={() => void submit("skip")}>本次跳过</Button>
        </Space>
      </Card>
    );
  }

  const shownArguments =
    typeof request.arguments === "string" ? request.arguments : JSON.stringify(request.arguments ?? {}, null, 2);
  return (
    <Card className="decision-card tool-decision" size="small" title={<><SafetyCertificateOutlined /> 工具审批</>}>
      <p>{request.message || `请求调用 ${request.tool || "工具"}`}</p>
      {request.tool ? <strong className="mono">{request.tool}</strong> : null}
      <pre>{shownArguments}</pre>
      <Input
        placeholder="补充说明（可选）"
        value={supplement}
        onChange={(event) => setSupplement(event.target.value)}
        disabled={submitting}
      />
      <Space className="decision-actions" wrap>
        <Button autoInsertSpace={false} type="primary" loading={submitting} disabled={submitting} onClick={() => void submit("continue")}>继续</Button>
        <Button autoInsertSpace={false} loading={submitting} disabled={submitting} onClick={() => void submit("allow_once")}>本次允许</Button>
        <Button autoInsertSpace={false} loading={submitting} disabled={submitting} onClick={() => void submit("allow_session")}>本会话允许</Button>
        <Button
          autoInsertSpace={false}
          disabled={submitting || !supplement.trim()}
          onClick={() => void submit("supplement", { supplement: supplement.trim() })}
        >
          提交补充
        </Button>
        <Button autoInsertSpace={false} disabled={submitting} onClick={() => void submit("cancel")}>取消</Button>
        <Button autoInsertSpace={false} danger disabled={submitting} onClick={() => void submit("deny")}>拒绝</Button>
      </Space>
    </Card>
  );
}
