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
    await user.click(screen.getByRole("button", { name: "压缩后实施" }));
    expect(onSubmit).toHaveBeenCalledWith("implement_and_compaction", {});
    await user.click(screen.getByRole("button", { name: "留在 Plan" }));
    expect(onSubmit).toHaveBeenCalledWith("stay_in_plan_mode", {});
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

  it("offers only the three sandbox approval outcomes", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <DecisionCard
        request={{ decision_id: "tool-1", kind: "tool", tool: "shell.exec", arguments: { command: "pwd" } }}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "本次允许",
      "本会话允许",
      "拒绝",
    ]);
    await user.click(screen.getByRole("button", { name: "本次允许" }));
    expect(onSubmit).toHaveBeenCalledWith("allow_once", {});
    await user.click(screen.getByRole("button", { name: "本会话允许" }));
    expect(onSubmit).toHaveBeenCalledWith("allow_session", {});
    await user.click(screen.getByRole("button", { name: "拒绝" }));
    expect(onSubmit).toHaveBeenCalledWith("deny", {});
  });

  it("renders a single-Skill trust review with trust and skip choices", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <DecisionCard
        request={{
          decision_id: "skill-1",
          kind: "skill",
          skill: "database-migration",
          description: "Applies the migration workflow.",
          project_id: "project_1",
          tree_sha256: "a".repeat(64),
          path: "C:/repo/.mini_agent/skills/database-migration",
        }}
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText("database-migration")).toBeInTheDocument();
    expect(screen.getByText(/a{64}/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /信任全部|信任当前版本并继续/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "信任这个 Skill" }));
    expect(onSubmit).toHaveBeenCalledWith("trust", {});

    onSubmit.mockClear();
    await user.click(screen.getByRole("button", { name: "本次跳过" }));
    expect(onSubmit).toHaveBeenCalledWith("skip", {});
  });

  it("renders malicious Skill text as plain content, not HTML", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <DecisionCard
        request={{
          decision_id: "skill-2",
          kind: "skill",
          skill: "evil",
          description: "<img src=x onerror=alert(1)>",
          tree_sha256: "b".repeat(64),
        }}
        onSubmit={onSubmit}
      />,
    );
    // The description is rendered as React text; no img element is created.
    expect(document.querySelector("img")).not.toBeInTheDocument();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
  });
});
