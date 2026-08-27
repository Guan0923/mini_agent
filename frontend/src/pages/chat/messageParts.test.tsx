import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage, DisplayMode, TurnItem } from "../../types";
import { AssistantMessage, MessageActions, summarizeReasoningTail, ToolLine } from "./messageParts";

describe("runtime thinking summary", () => {
  it("normalizes whitespace into one line", () => {
    expect(summarizeReasoningTail("\n\n  第一段思考  \n\n第二段\t继续 ")).toBe("第一段思考 第二段 继续");
  });

  it("keeps exactly two hundred and fifty Unicode graphemes without an ellipsis", () => {
    const value = "中😀".repeat(125);
    expect(Array.from(summarizeReasoningTail(value))).toHaveLength(250);
    expect(summarizeReasoningTail(value)).toBe(value);
  });

  it("keeps the last two hundred and fifty graphemes and adds one leading ellipsis", () => {
    const tail = "👨‍👩‍👧‍👦".repeat(250);
    const summary = summarizeReasoningTail(`应被截断的前缀${tail}`);
    expect(summary).toBe(`…${tail}`);
  });

  it("returns an empty fallback signal for blank content", () => {
    expect(summarizeReasoningTail(" \n\t ")).toBe("");
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

  it("keeps thinking Markdown compact without changing regular paragraph spacing", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat.css`, "utf8");
    const rule = css.slice(css.indexOf(".thinking-content {"), css.indexOf(".shimmer-text {"));

    expect(rule).toMatch(/\.thinking-content\s*{[^}]*line-height:\s*1\.5;/s);
    expect(rule).toMatch(/\.thinking-content\s*{[^}]*white-space:\s*normal;/s);
    expect(rule).toMatch(/\.thinking-content \.markdown\s*{[^}]*white-space:\s*normal;/s);
    expect(css).toMatch(/\.thinking-content \.markdown p\s*{[^}]*margin:\s*0;/s);

    const { container } = render(
      <>
        <style>{css}</style>
        <div className="thinking-content"><div className="markdown"><p>第一段</p><p>第二段</p></div></div>
        <div className="markdown regular-markdown"><p>普通第一段</p><p>普通第二段</p></div>
      </>,
    );
    const thinking = container.querySelector<HTMLElement>(".thinking-content")!;
    const thinkingParagraph = container.querySelector<HTMLElement>(".thinking-content .markdown p")!;
    const regularParagraph = container.querySelector<HTMLElement>(".regular-markdown p")!;
    expect(window.getComputedStyle(thinking).lineHeight).toBe("1.5");
    expect(window.getComputedStyle(thinking).whiteSpace).toBe("normal");
    expect(window.getComputedStyle(thinkingParagraph).marginBottom).toBe("0px");
    expect(window.getComputedStyle(regularParagraph).marginBottom).toBe("10px");
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

function renderAssistant(message: ChatMessage, display: DisplayMode = "developer") {
  return (
    <AntApp>
      <AssistantMessage
        msg={message}
        display={display}
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

  it.each<DisplayMode>(["medium", "verbose", "developer"])(
    "starts every runtime Collapse folded in %s mode",
    (display) => {
      const activeItems: TurnItem[] = [
        { type: "reasoning", text: "实时思考" },
        { type: "tool_call", call_id: "call-folded", name: "read_file", arguments: {} },
        { type: "tool_result", call_id: "call-folded", tool: "read_file", content: "实时结果" },
      ];

      for (const item of activeItems) {
        const view = render(renderAssistant(assistant([item], true), display));
        expect(view.container.querySelector(".runtime-item-collapse .ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
        view.unmount();
      }
    },
  );

  it("keeps manual expansion across active changes while new Items stay folded", async () => {
    const first: TurnItem = { type: "reasoning", text: "流式思考" };
    const tool: TurnItem = { type: "tool_call", call_id: "call-1", name: "read_file", arguments: {} };
    const result: TurnItem = { type: "tool_result", call_id: "call-1", tool: "read_file", content: "读取完成" };
    const view = render(renderAssistant(assistant([first], true)));

    let collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("流式思考");
    expect(collapses[0].querySelectorAll(".runtime-status-dot")).toHaveLength(0);
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    fireEvent.click(collapses[0].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("正在思考中");
    expect(collapses[0].querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    const updatedFirst: TurnItem = { type: "reasoning", text: "流式思考继续" };
    view.rerender(renderAssistant(assistant([updatedFirst], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("正在思考中");
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();

    view.rerender(renderAssistant(assistant([updatedFirst, tool], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[0].querySelector(".shimmer-text.is-active")).toBeNull();
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("思考详情");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("正在调用 read_file");
    expect(collapses[1].querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(collapses[1].querySelector(".shimmer-text.is-active")).toHaveTextContent("正在调用 read_file");

    fireEvent.click(collapses[1].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[1].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[1].querySelector(".shimmer-text.is-active")).toBeNull();
    expect(collapses[1].querySelectorAll(".runtime-status-dot")).toHaveLength(3);

    view.rerender(renderAssistant(assistant([updatedFirst, tool, result], true)));
    collapses = view.container.querySelectorAll(".runtime-item-collapse");
    expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active");
    expect(collapses[2].querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(collapses[2].querySelector(".ant-collapse-header")).toHaveTextContent("正在处理 read_file 结果");
    expect(collapses[2].querySelector(".shimmer-text.is-active")).toHaveTextContent("正在处理 read_file 结果");

    fireEvent.click(collapses[2].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[2].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
  });

  it("keeps folded reasoning summaries pinned to the right edge", async () => {
    let resizeCallback: ResizeObserverCallback | undefined;
    class MockResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }

      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    const originalResizeObserver = window.ResizeObserver;
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

    const view = render(renderAssistant(assistant([{ type: "reasoning", text: "初始思考" }], true)));
    const collapse = view.container.querySelector(".runtime-item-collapse")!;
    expect(collapse.querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");

    const viewport = collapse.querySelector<HTMLElement>(".runtime-summary-viewport")!;
    const summaryText = viewport.querySelector(".runtime-summary-text");
    expect(viewport.querySelector(".shimmer-text")).toBeNull();
    let clientWidth = 120;
    let scrollWidth = 80;
    Object.defineProperties(viewport, {
      clientWidth: { configurable: true, get: () => clientWidth },
      scrollWidth: { configurable: true, get: () => scrollWidth },
    });

    viewport.scrollLeft = 42;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "短摘要更新" }], true)));
    expect(viewport.scrollLeft).toBe(0);
    expect(collapse.querySelector(".runtime-summary-viewport")).toBe(viewport);
    expect(viewport.querySelector(".runtime-summary-text")).toBe(summaryText);
    expect(viewport.querySelector(".shimmer-text")).toBeNull();

    clientWidth = 100;
    scrollWidth = 260;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "足够长的摘要更新并贴住最新字符" }], true)));
    expect(viewport.scrollLeft).toBe(160);

    clientWidth = 150;
    resizeCallback?.([], {} as ResizeObserver);
    expect(viewport.scrollLeft).toBe(110);

    clientWidth = 90;
    scrollWidth = 240;
    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "已完成且仍然跟随尾部" }], false)));
    const completedViewport = view.container.querySelector<HTMLElement>(".runtime-summary-viewport")!;
    Object.defineProperties(completedViewport, {
      clientWidth: { configurable: true, get: () => clientWidth },
      scrollWidth: { configurable: true, get: () => scrollWidth },
    });
    resizeCallback?.([], {} as ResizeObserver);
    expect(completedViewport.scrollLeft).toBe(150);
    expect(completedViewport.querySelector(".shimmer-text.is-active")).toBeNull();
    expect(completedViewport.querySelector(".runtime-summary-text")).toHaveTextContent("已完成且仍然跟随尾部");

    window.ResizeObserver = originalResizeObserver;
  });

  it("uses static completed labels and distinguishes failed tool results", async () => {
    const items: TurnItem[] = [
      { type: "reasoning", text: "完成后的思考摘要" },
      { type: "tool_call", name: "read_file", arguments: {} },
      { type: "tool_result", tool: "read_file", content: "成功结果", status: "succeeded" },
      { type: "tool_result", tool: "write_file", content: "失败结果", status: "failed" },
    ];
    const { container } = render(renderAssistant(assistant(items)));
    const collapses = container.querySelectorAll(".runtime-item-collapse");

    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("完成后的思考摘要");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(collapses[2].querySelector(".ant-collapse-header")).toHaveTextContent("read_file 结果");
    expect(collapses[3].querySelector(".ant-collapse-header")).toHaveTextContent("write_file 失败");

    fireEvent.click(collapses[0].querySelector(".ant-collapse-header")!);
    fireEvent.click(collapses[1].querySelector(".ant-collapse-header")!);
    await waitFor(() => expect(collapses[0].querySelector(".ant-collapse-item")).toHaveClass("ant-collapse-item-active"));
    expect(collapses[0].querySelector(".ant-collapse-header")).toHaveTextContent("思考详情");
    expect(collapses[1].querySelector(".ant-collapse-header")).toHaveTextContent("调用 read_file");
    expect(container.querySelector(".shimmer-text.is-active")).toBeNull();
  });

  it("falls back to the active reasoning status when folded content is empty", () => {
    const { container } = render(renderAssistant(assistant([{ type: "reasoning", text: "" }], true)));
    const collapse = container.querySelector(".runtime-item-collapse")!;
    expect(collapse.querySelector(".ant-collapse-item")).not.toHaveClass("ant-collapse-item-active");
    expect(collapse.querySelector(".shimmer-text.is-active")).toHaveTextContent("正在思考中");
    expect(collapse.querySelectorAll(".runtime-status-dot")).toHaveLength(3);
  });

  it("renders only the current non-collapsible status in minimal mode", () => {
    const view = render(renderAssistant(assistant([
      { type: "reasoning", text: "历史思考" },
      { type: "tool_call", name: "read_file", arguments: { path: "README.md" } },
      { type: "tool_result", tool: "read_file", content: "隐藏结果", status: "succeeded" },
    ], true), "minimal"));

    expect(view.container.querySelector(".runtime-item-collapse")).toBeNull();
    expect(view.container.querySelectorAll(".runtime-minimal-status")).toHaveLength(1);
    expect(screen.getByRole("status", { name: "正在处理 read_file 结果" })).toBeInTheDocument();
    expect(view.container.querySelectorAll(".runtime-status-dot")).toHaveLength(3);
    expect(view.container).not.toHaveTextContent("历史思考");
    expect(view.container).not.toHaveTextContent("隐藏结果");

    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "实时思考" }], true), "minimal"));
    expect(screen.getByRole("status", { name: "思考中" })).toBeInTheDocument();
    expect(view.container).not.toHaveTextContent("实时思考");

    view.rerender(renderAssistant(assistant([{ type: "reasoning", text: "完成思考" }]), "minimal"));
    expect(view.container.querySelector(".runtime-minimal-status")).toBeNull();
    expect(view.container.querySelector(".runtime-item-collapse")).toBeNull();
  });

  it("uses one non-repeating shimmer band and honors reduced motion for both animations", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat.css`, "utf8");
    const activeRule = css.slice(css.indexOf(".shimmer-text.is-active"), css.indexOf("@keyframes runtime-summary-shimmer"));
    const reducedMotion = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));

    expect(activeRule).toContain("#ffffff");
    expect(activeRule).toMatch(/animation:\s*runtime-summary-shimmer/);
    expect(activeRule).toMatch(/background-repeat:\s*no-repeat/);
    expect(css).toMatch(/\.runtime-status-dot\s*{[^}]*animation:\s*runtime-status-dot 900ms ease-in-out infinite;/s);
    expect(css).toMatch(/\.runtime-status-dot:nth-child\(2\)\s*{[^}]*animation-delay:\s*120ms;/s);
    expect(css).toMatch(/\.runtime-status-dot:nth-child\(3\)\s*{[^}]*animation-delay:\s*240ms;/s);
    expect(css).toMatch(/@keyframes runtime-status-dot[\s\S]*transform:\s*translateY\(-3px\);[\s\S]*background-color:\s*#ffffff;/);
    expect(reducedMotion).toMatch(/\.shimmer-text\.is-active\s*{[^}]*animation:\s*none;/s);
    expect(reducedMotion).toMatch(/-webkit-text-fill-color:\s*currentColor/);
    expect(reducedMotion).toMatch(/\.runtime-status-dot\s*{[^}]*animation:\s*none;/s);
  });

  it("uses one unclipped summary track and matching header and body padding", async () => {
    const fs = await vi.importActual<{ readFileSync(path: string, encoding: "utf8"): string }>("node:fs");
    const runtime = globalThis as typeof globalThis & { process: { cwd(): string } };
    const css = fs.readFileSync(`${runtime.process.cwd()}/src/styles/chat.css`, "utf8");

    expect(css).toMatch(/\.runtime-collapse\s*{[^}]*--runtime-collapse-inline-padding:\s*12px;/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-header\s*{[^}]*padding-inline:\s*var\(--runtime-collapse-inline-padding\)/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-body\s*{[^}]*padding-inline:\s*var\(--runtime-collapse-inline-padding\)/s);
    expect(css).toMatch(/\.runtime-collapse \.ant-collapse-expand-icon\s*{[^}]*position:\s*absolute;[^}]*inset-inline-start:\s*0;/s);
    expect(css).toMatch(/\.runtime-summary-viewport\s*{[^}]*width:\s*100%;[^}]*overflow:\s*hidden;/s);
    expect(css).toMatch(/\.runtime-summary-track\s*{[^}]*width:\s*max-content;[^}]*white-space:\s*nowrap;/s);
    expect(css).toMatch(/\.runtime-summary-viewport \.runtime-summary-text\s*{[^}]*overflow:\s*visible;[^}]*text-overflow:\s*clip;/s);
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
