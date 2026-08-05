import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import PublicLayout from "./PublicLayout";

vi.mock("../components/OceanScene", () => ({
  default: () => <div data-testid="ocean-scene" />,
}));

function PublicRouteFixture() {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route
          path="/"
          element={
            <main>
              <h1>首页</h1>
              <Link to="/login">登录</Link>
              <Link to="/register">注册</Link>
            </main>
          }
        />
        <Route path="/login" element={<h1>登录卡片</h1>} />
        <Route path="/register" element={<h1>注册卡片</h1>} />
      </Route>
    </Routes>
  );
}

describe("PublicLayout", () => {
  it("mounts one ocean scene and keeps it mounted as the foreground route changes", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    const scene = screen.getByTestId("ocean-scene");
    expect(container.querySelectorAll('[data-testid="ocean-scene"]')).toHaveLength(1);
    fireEvent.click(screen.getByRole("link", { name: "登录" }));
    expect(screen.getByRole("heading", { name: "登录卡片" })).toBeInTheDocument();
    expect(screen.getByTestId("ocean-scene")).toBe(scene);
    expect(container.querySelectorAll('[data-testid="ocean-scene"]')).toHaveLength(1);
  });

  it("renders the matching card for a deep link", () => {
    render(
      <MemoryRouter initialEntries={["/register"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "注册卡片" })).toBeInTheDocument();
    expect(screen.getByTestId("ocean-scene")).toBeInTheDocument();
  });
});
