import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AuthLayout from "./AuthLayout";

describe("AuthLayout", () => {
  it("renders authentication content over the shared public background", () => {
    render(
      <MemoryRouter>
        <AuthLayout title="欢迎回来" subtitle="登录后继续你的智能体工作流。">
          <button type="button">继续</button>
        </AuthLayout>
      </MemoryRouter>,
    );

    expect(screen.queryByTestId("ocean-scene")).toBeNull();
    const heading = screen.getByRole("heading", { name: "欢迎回来" });
    const card = heading.closest(".auth-card");
    expect(card).toHaveClass("auth-card--staggered-reveal");
    expect(card?.querySelector(".auth-card-content")).toContainElement(screen.getByRole("button", { name: "继续" }));
    expect(screen.getByText("登录后继续你的智能体工作流。")).toBeTruthy();
    expect(screen.getByRole("link", { name: "← 返回首页" }).closest(".auth-back")).toHaveClass("auth-back--reveal");
  });
});
