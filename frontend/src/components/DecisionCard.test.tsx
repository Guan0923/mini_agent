import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DecisionCard from "./DecisionCard";

describe("DecisionCard", () => {
  it("keeps plan decision choice protocol while using Card and Buttons", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <DecisionCard
        request={{ decision_id: "plan-1", kind: "plan", plan: "1. Inspect the workspace" }}
        onSubmit={onSubmit}
      />,
    );

    await user.click(screen.getByRole("button", { name: "实施" }));
    expect(onSubmit).toHaveBeenCalledWith("implement", {});
  });

  it("submits selected question answers through the answers protocol", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <DecisionCard
        request={{
          decision_id: "question-1",
          kind: "question",
          questions: [{
            id: "permission",
            header: "权限",
            question: "选择权限模式",
            options: [{ label: "完全访问", description: "自动批准工具" }],
          }],
        }}
        onSubmit={onSubmit}
      />,
    );

    await user.click(screen.getByRole("radio", { name: /完全访问/ }));
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    expect(onSubmit).toHaveBeenCalledWith("answer", { answers: { permission: ["完全访问"] } });
  });

  it("requires supplement text before sending a tool supplement", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <DecisionCard
        request={{ decision_id: "tool-1", kind: "tool", tool: "shell.exec", arguments: { command: "pwd" } }}
        onSubmit={onSubmit}
      />,
    );

    const supplement = screen.getByPlaceholderText("补充说明（可选）");
    expect(screen.getByRole("button", { name: "提交补充" })).toBeDisabled();
    await user.type(supplement, "仅查看当前目录");
    await user.click(screen.getByRole("button", { name: "提交补充" }));
    expect(onSubmit).toHaveBeenCalledWith("supplement", { supplement: "仅查看当前目录" });
  });
});
