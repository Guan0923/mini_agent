import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import HomePage from "./HomePage";

vi.mock("../components/OceanScene", () => ({ default: () => <div data-testid="ocean-scene" /> }));

describe("HomePage", () => {
  it("shows the ocean canvas region and both authentication paths", () => {
    render(<MemoryRouter><HomePage /></MemoryRouter>);
    expect(screen.getByTestId("ocean-scene")).toBeTruthy();
    expect(screen.getAllByRole("link", { name: "登录" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: /注册/ }).length).toBeGreaterThan(0);
  });
});
