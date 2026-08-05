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
    expect(screen.getAllByRole("link", { name: "登录" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /注册/ }).length).toBeGreaterThan(0);
  });
});
