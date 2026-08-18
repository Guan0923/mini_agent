import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import HomePage from "./HomePage";

describe("HomePage", () => {
  it("shows a continuous centered hero and both authentication paths", () => {
    render(<MemoryRouter><HomePage /></MemoryRouter>);
    const heading = screen.getByRole("heading", { name: /让想法像潮汐一样/ });
    expect(heading.textContent).toBe("让想法像潮汐一样，自然地流动。");
    expect(heading.querySelector("br")).toBeNull();
    expect(screen.queryByText("左右移动鼠标，感受波浪")).toBeNull();
    expect(screen.getByRole("navigation", { name: "账户导航" }).querySelector('a[href="/login"]')).toHaveClass("outline-cta");
    expect(screen.getByRole("navigation", { name: "账户导航" }).querySelector('a[href="/register"]')).toHaveClass("text-link");
    expect(screen.getByRole("navigation", { name: "账户导航" })).toHaveClass("home-reveal", "home-reveal--nav");
    expect(screen.getByText("A calmer way to build with agents")).toHaveClass("home-reveal", "home-reveal--eyebrow");
    expect(screen.getByRole("heading", { name: /让想法像潮汐一样/ })).toHaveClass("home-reveal", "home-reveal--title");
    expect(screen.getByText(/Mini-Agent 将规划/)).toHaveClass("home-reveal", "home-reveal--description");
    expect(screen.getByText("规划 · 工具 · 技能 · 可观察执行").closest("footer")).toHaveClass("home-reveal", "home-reveal--footer");
    expect(screen.getByRole("link", { name: /开始探索/ })).toHaveAttribute("href", "/login");
  });
});
