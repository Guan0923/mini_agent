import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AuthLayout from "./AuthLayout";
import LoginPage from "./LoginPage";
import RegisterPage from "./RegisterPage";
import ResetPasswordPage from "./ResetPasswordPage";
import "../../styles.css";

vi.mock("../../components/OceanScene", () => ({ default: () => <div data-testid="ocean-scene" /> }));
vi.mock("../../auth/AuthProvider", () => ({
  useAuth: () => ({ signIn: vi.fn(), setUser: vi.fn() }),
}));

describe("Auth form spacing", () => {
  it("keeps the shared auth form labels compact and fields separated by 16px", () => {
    const { container } = render(
      <MemoryRouter>
        <AuthLayout title="创建你的账号" subtitle="开始新的工作流。">
          <form className="auth-form">
            <div>
              <label htmlFor="auth-email">邮箱</label>
              <input id="auth-email" />
            </div>
          </form>
        </AuthLayout>
      </MemoryRouter>,
    );

    const card = container.querySelector(".auth-card");
    const form = container.querySelector(".auth-form");
    const field = container.querySelector(".auth-form > div");
    const label = screen.getByText("邮箱");
    expect(card).toBeInTheDocument();
    expect(form).toBeInTheDocument();
    expect(field).toBeInTheDocument();
    expect(getComputedStyle(label).display).not.toBe("flex");
  });

  it("uses the shared auth card and form scope on every auth page", () => {
    for (const page of [<LoginPage />, <RegisterPage />, <ResetPasswordPage />]) {
      const { container } = render(<MemoryRouter>{page}</MemoryRouter>);
      const card = container.querySelector(".auth-card");
      const form = container.querySelector(".auth-card .auth-form");
      expect(card).toBeInTheDocument();
      expect(form).toBeInTheDocument();
      expect(container.querySelectorAll(".auth-card")).toHaveLength(1);
      expect(container.querySelectorAll(".auth-form")).toHaveLength(1);
    }
  });

  it("aligns verification OTP controls in register and reset forms", () => {
    for (const page of [<RegisterPage />, <ResetPasswordPage />]) {
      const { container, unmount } = render(<MemoryRouter>{page}</MemoryRouter>);
      const row = container.querySelector(".code-row");
      const field = row?.querySelector(".code-field");
      const button = row?.querySelector(".code-button");

      expect(row).toBeInTheDocument();
      expect(field).toBeInTheDocument();
      expect(button).toBeInTheDocument();
      expect(getComputedStyle(field as Element).marginBottom).toBe("0px");
      expect(getComputedStyle(row as Element).marginBottom).toBe("16px");
      expect(getComputedStyle(button as Element).height).toBe("48px");
      unmount();
    }
  });
});