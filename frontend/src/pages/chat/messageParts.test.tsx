import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../../types";
import { AssistantMessage, summarizeThinking } from "./messageParts";

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

    const firstHeader = collapses[0].querySelector(".ant-collapse-header");
    expect(firstHeader).not.toBeNull();
    fireEvent.click(firstHeader as HTMLElement);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));

    expect(screen.getByText("模型思考内容")).toBeInTheDocument();
    expect(screen.getByText("工具结果一")).toBeInTheDocument();
    expect(screen.getByText("最终答案")).toBeInTheDocument();
    expect(screen.getByText("最终答案").closest(".runtime-collapse")).toBeNull();
  });
});
