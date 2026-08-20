import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import { AssistantMessage, summarizeThinking, ToolLine } from "./messageParts";

describe("runtime thinking summary", () => {
  it("uses the first non-empty paragraph and trims its edges", () => {
    expect(summarizeThinking("\n\n  第一段思考  \n\n第二段")).toBe("第一段思考");
  });

  it("keeps exactly one hundred Unicode code points without an ellipsis", () => {
    const value = "中😀".repeat(50);
    expect(Array.from(summarizeThinking(value))).toHaveLength(100);
    expect(summarizeThinking(value)).toBe(value);
  });

  it("adds five ASCII periods only when the paragraph is longer", () => {
    const value = "中文😀".repeat(51);
    const summary = summarizeThinking(value);
    expect(summary.slice(0, -5)).toBe(Array.from(value).slice(0, 100).join(""));
    expect(summary.endsWith(".....")).toBe(true);
  });
});

describe("assistant runtime Collapse presentation", () => {
  it("keeps runtime details collapsible while separating them from the answer", async () => {
    const message: ChatMessage = {
      id: "assistant-runtime",
      role: "assistant",
      content: "",
      events: [],
      segments: [
        {
          sequence: 1,
          segment_id: "thinking-1",
          segment_type: "thinking",
          status: "completed",
          text: "模型思考内容",
        },
        {
          sequence: 2,
          segment_id: "tools-1",
          segment_type: "tool_batch",
          status: "completed",
          tools: [
            { call_id: "call-1", name: "读取文件", arguments: { path: "README.md" }, status: "succeeded", result: "工具结果一" },
            { call_id: "call-2", name: "搜索文本", arguments: { query: "Collapse" }, status: "succeeded", result: "工具结果二" },
          ],
        },
        {
          sequence: 3,
          segment_id: "response-1",
          segment_type: "response",
          status: "completed",
          text: "最终答案",
        },
      ],
    };

    const { container } = render(
      <AntApp>
        <AssistantMessage
          msg={message}
          display="developer"
          busy={false}
          onDecision={vi.fn().mockResolvedValue(undefined)}
        />
      </AntApp>,
    );

    const collapses = container.querySelectorAll(".runtime-collapse");
    expect(collapses).toHaveLength(2);

    const toolBatchHeader = collapses[1].querySelector(".ant-collapse-header");
    expect(toolBatchHeader).not.toBeNull();
    fireEvent.click(toolBatchHeader as HTMLElement);
    await waitFor(() => expect(container.querySelectorAll(".runtime-collapse")).toHaveLength(3));

    const expandedCollapses = container.querySelectorAll(".runtime-collapse");
    const nestedToolHeader = expandedCollapses[2].querySelector(".ant-collapse-header");
    expect(nestedToolHeader).not.toBeNull();
    fireEvent.click(nestedToolHeader as HTMLElement);
    const runtimeResult = container.querySelector("pre.tool-result");
    expect(runtimeResult).not.toBeNull();
    expect(runtimeResult).toHaveTextContent("工具结果一");

    const firstHeader = collapses[0].querySelector(".ant-collapse-header");
    expect(firstHeader).not.toBeNull();
    fireEvent.click(firstHeader as HTMLElement);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));

    expect(screen.getByText("模型思考内容")).toBeInTheDocument();
    expect(screen.getByText("工具结果一")).toBeInTheDocument();
    expect(screen.getByText("最终答案")).toBeInTheDocument();
    expect(screen.getByText("最终答案").closest(".runtime-collapse")).toBeNull();
  });

  it("keeps legacy tool results inside a pre block", () => {
    const result = "第一行\n第二行\n第三行\n第四行\n第五行\n第六行";
    const { container } = render(
      <ToolLine
        ev={{ kind: "tool_result", message: result, data: { tool: "读取文件", result } }}
        display="developer"
      />,
    );

    const resultBlock = container.querySelector(".tool-result > pre");
    expect(resultBlock).not.toBeNull();
    expect(resultBlock?.textContent).toBe(result);
    const payloadBlock = container.querySelector("pre.tool-payload");
    expect(payloadBlock).not.toBeNull();
    expect(payloadBlock).not.toHaveClass("tool-result");
  });

  it("keeps final answer Markdown code blocks separate from tool results", () => {
    const message: ChatMessage = {
      id: "assistant-markdown-code",
      role: "assistant",
      content: "```text\n最终答案代码\n```",
      events: [],
      segments: [],
    };

    const { container } = render(
      <AntApp>
        <AssistantMessage
          msg={message}
          display="verbose"
          busy={false}
          onDecision={vi.fn().mockResolvedValue(undefined)}
        />
      </AntApp>,
    );

    const answerCode = container.querySelector(".markdown pre");
    expect(answerCode).not.toBeNull();
    expect(answerCode).not.toHaveClass("tool-result");
    expect(answerCode).toHaveTextContent("最终答案代码");
  });

  it("shows a denied tool without exposing model feedback or skipped batch tools", async () => {
    const message: ChatMessage = {
      id: "assistant-denied",
      role: "assistant",
      content: "继续回复",
      events: [],
      segments: [
        {
          sequence: 1,
          segment_id: "tools-denied",
          segment_type: "tool_batch",
          status: "failed",
          tools: [
            {
              call_id: "call-denied",
              name: "write_file",
              arguments: { path: "a.txt", content: "" },
              status: "failed",
              error: "The user denied this write_file tool call.",
              failure_code: "user_denied",
            },
            {
              call_id: "call-skipped",
              name: "run_command",
              arguments: { command: "echo skipped" },
              status: "failed",
              error: "Not executed because tool execution was interrupted.",
              failure_code: "user_denied_batch",
            },
          ],
        },
      ],
    };

    const { container } = render(
      <AntApp>
        <AssistantMessage
          msg={message}
          display="developer"
          busy={false}
          onDecision={vi.fn().mockResolvedValue(undefined)}
        />
      </AntApp>,
    );

    const header = screen.getByText("write_file · 已拒绝");
    fireEvent.click(header);
    await waitFor(() => expect(screen.getByText("已拒绝")).toBeInTheDocument());
    expect(container).not.toHaveTextContent("The user denied this write_file tool call.");
    expect(container).not.toHaveTextContent("run_command");
  });

  it("keeps a denial visible in minimal legacy details", () => {
    const { container } = render(
      <ToolLine
        ev={{
          kind: "tool_failed",
          message: "The user denied this write_file tool call.",
          data: { tool: "write_file", call_id: "call-denied", failure_code: "user_denied" },
        }}
        display="minimal"
      />,
    );

    expect(container).toHaveTextContent("write_file");
    expect(container).toHaveTextContent("已拒绝");
    expect(container).not.toHaveTextContent("The user denied this write_file tool call.");
  });
});
