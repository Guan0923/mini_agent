import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage, TurnItem } from "../../types";
import { AssistantMessage, MessageActions, summarizeThinking, ToolLine } from "./messageParts";

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

describe("message actions", () => {
  it("removes the user rewind action while retaining copy and edit", () => {
    render(
      <AntApp>
        <MessageActions
          msg={{ id: "user-1", role: "user", content: "hello", events: [] }}
          busy={false}
          onEdit={vi.fn()}
        />
      </AntApp>,
    );

    expect(screen.getByRole("button", { name: "复制" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "回溯" })).not.toBeInTheDocument();
  });

  it("keeps thinking Markdown at 1.5 line height without pre-wrapped outer whitespace", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat.css`, "utf8");
    const rule = css.slice(css.indexOf(".thinking-content {"), css.indexOf(".shimmer-text {"));

    expect(rule).toMatch(/\.thinking-content\s*{[^}]*line-height:\s*1\.5;/s);
    expect(rule).toMatch(/\.thinking-content\s*{[^}]*white-space:\s*normal;/s);
    expect(rule).toMatch(/\.thinking-content \.markdown\s*{[^}]*white-space:\s*normal;/s);

    const { container } = render(
      <>
        <style>{css}</style>
        <div className="thinking-content"><div className="markdown">第一行<br />第二行</div></div>
      </>,
    );
    const thinking = container.querySelector<HTMLElement>(".thinking-content")!;
    expect(window.getComputedStyle(thinking).lineHeight).toBe("1.5");
    expect(window.getComputedStyle(thinking).whiteSpace).toBe("normal");
  });
});

function assistant(items: TurnItem[], running = false): ChatMessage {
  return {
    id: "assistant-runtime",
    role: "assistant",
    content: items.filter((item) => item.type === "text").map((item) => String(item.text ?? "")).join(""),
    events: [],
    items,
    itemVersion: 0,
    running,
    status: running ? "running" : "success",
  };
}

function renderAssistant(message: ChatMessage) {
  return (
    <AntApp>
      <AssistantMessage
        msg={message}
        display="developer"
        busy={false}
        onDecision={vi.fn().mockResolvedValue(undefined)}
      />
    </AntApp>
  );
}

describe("assistant Item presentation", () => {
  it("hides Skill metadata and shows the running indicator instead of none", () => {
    render(renderAssistant(assistant([
      { type: "skill_snapshot", event: "skills_selected", text: "none", skills: [] },
    ], true)));

    expect(screen.queryByText("none")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "思考中" })).toBeInTheDocument();
  });

  it("renders one pending tool approval card", () => {
    const message = assistant([
      { type: "approval", event: "decision_requested", decision_id: "dec-search", kind: "tool", call_id: "call-search", tool: "web_search", arguments: { query: "local" }, text: "Call tool web_search?" },
    ], true);
    message.decision = {
      decision_id: "dec-search",
      kind: "tool",
      tool: "web_search",
      arguments: { query: "local" },
      message: "Call tool web_search?",
    };
    const { container } = render(renderAssistant(message));

    expect(screen.getAllByText("Call tool web_search?")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "本次允许" })).toHaveLength(1);
    expect(container.querySelectorAll('[data-item-type="approval"]')).toHaveLength(1);
  });

  it("renders resolved approval once in canonical Item order", () => {
    const { container } = render(renderAssistant(assistant([
      { type: "tool_call", call_id: "call-search", name: "web_search", arguments: { query: "local" } },
      { type: "approval", event: "approval_resolved", approval_status: "allowed", call_id: "call-search", tool: "web_search" },
      { type: "tool_result", call_id: "call-search", tool: "web_search", content: "local result", status: "succeeded" },
      { type: "text", text: "done" },
    ])));

    expect(screen.getAllByText("已允许 web_search")).toHaveLength(1);
    const runtimeItems = container.querySelector(".runtime-items");
    expect(Array.from(runtimeItems!.children).map((element) => (element as HTMLElement).dataset.itemType)).toEqual([
      "tool_call",
      "approval",
      "tool_result",
      "text",
    ]);
  });

  it("renders a denied approval as one static status", () => {
    render(renderAssistant(assistant([
      { type: "approval", event: "approval_resolved", approval_status: "denied", call_id: "call-search", tool: "web_search" },
    ])));

    expect(screen.getAllByText("已拒绝 web_search")).toHaveLength(1);
    expect(screen.queryByText("Call tool web_search?")).not.toBeInTheDocument();
  });

  it("renders every Item in canonical order and keeps answers outside Collapse", async () => {
    const items: TurnItem[] = [
      { type: "reasoning", text: "第一次思考" },
      { type: "tool_call", call_id: "call-1", name: "read_file", arguments: { path: "README.md" } },
      { type: "tool_result", call_id: "call-1", tool: "read_file", content: "工具结果" },
      { type: "text", text: "中间回答" },
      { type: "reasoning", text: "第二次思考" },
      { type: "tool_call", call_id: "call-2", name: "glob", arguments: { pattern: "*.ts" } },
      { type: "text", text: "最终回答" },
    ];
    const { container } = render(renderAssistant(assistant(items)));

    const runtimeItems = container.querySelector(".runtime-items");
    expect(runtimeItems).not.toBeNull();
    expect(Array.from(runtimeItems!.children).map((element) => (element as HTMLElement).dataset.itemType)).toEqual([
      "reasoning",
      "tool_call",
      "tool_result",
      "text",
      "reasoning",
      "tool_call",
      "text",
    ]);
    expect(container.querySelectorAll(".runtime-item-collapse")).toHaveLength(5);
    expect(screen.getByText("中间回答").closest(".runtime-collapse")).toBeNull();
    expect(screen.getByText("最终回答").closest(".runtime-collapse")).toBeNull();

    const resultCollapse = container.querySelector('[data-item-type="tool_result"]');
    fireEvent.click(resultCollapse!.querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(resultCollapse!.querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(resultCollapse).toHaveTextContent("工具结果");
  });

  it("opens only the current Item, folds it when the next Item arrives, and preserves manual reopening", async () => {
    const first: TurnItem = { type: "reasoning", text: "流式思考" };
    const tool: TurnItem = { type: "tool_call", call_id: "call-1", name: "read_file", arguments: {} };
    const view = render(renderAssistant(assistant([first], true)));

    let collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toHaveTextContent("思考");

    view.rerender(renderAssistant(assistant([first, tool], true)));
    await waitFor(() => {
      collapses = view.container.querySelectorAll(".runtime-item-collapse");
      expect(collapses[0].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
      expect(collapses[1].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    });
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();
    expect(collapses[1].querySelector(".shimmer-text.is-active")).toHaveTextContent("调用 read_file");

    fireEvent.click(collapses[0].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    view.rerender(renderAssistant(assistant([first, tool], true)));
    expect(view.container.querySelectorAll(".runtime-item-collapse")[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");

    view.rerender(renderAssistant(assistant([first, tool], false)));
    await waitFor(() => expect(view.container.querySelectorAll(".runtime-item-collapse")[1].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active"));
    expect(view.container.querySelector(".shimmer-text.is-active")).toBeNull();
  });

  it("uses white shimmer only for active Collapse titles and honors reduced motion", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat.css`, "utf8");
    const activeRule = css.slice(css.indexOf(".shimmer-text.is-active"), css.indexOf("@keyframes runtime-summary-shimmer"));
    const reducedMotion = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));

    expect(activeRule).toContain("#ffffff");
    expect(activeRule).toMatch(/animation:\s*runtime-summary-shimmer/);
    expect(reducedMotion).toMatch(/\.shimmer-text\.is-active\s*{[^}]*animation:\s*none;/s);
    expect(reducedMotion).toMatch(/-webkit-text-fill-color:\s*currentColor/);
  });

  it("keeps tool results inside a pre block in developer mode", () => {
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

  it("keeps answer Markdown code blocks separate from tool results", () => {
    const message = assistant([{ type: "text", text: "```text\n最终答案代码\n```" }]);
    const { container } = render(renderAssistant(message));

    const answerCode = container.querySelector(".markdown pre");
    expect(answerCode).not.toBeNull();
    expect(answerCode).not.toHaveClass("tool-result");
    expect(answerCode).toHaveTextContent("最终答案代码");
  });

  it("keeps a denied tool visible without exposing model feedback", () => {
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

  it("renders non-denial tool failures inside their Item", () => {
    const { container } = render(
      <ToolLine
        ev={{
          kind: "tool_failed",
          message: "command failed",
          data: { tool: "run_command", call_id: "call-failed", result: "exit code 1" },
        }}
        display="verbose"
      />,
    );

    expect(container).toHaveTextContent("run_command");
    expect(container).toHaveTextContent("失败");
    expect(container).toHaveTextContent("exit code 1");
  });
});
