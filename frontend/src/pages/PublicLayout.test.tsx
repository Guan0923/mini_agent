import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate, useOutletContext } from "react-router-dom";
import type { OceanTransition } from "../components/OceanScene";
import PublicLayout, { AUTH_EFFECTS_STORAGE_KEY, type PublicOutletContext } from "./PublicLayout";
import AuthLayout, { AuthTransitionLink } from "./auth/AuthLayout";

const oceanMock = vi.hoisted(() => ({
  props: null as null | {
    transition?: OceanTransition | null;
    effectsEnabled?: boolean;
    onFallbackChange?: (fallback: boolean) => void;
  },
}));

vi.mock("../components/OceanScene", () => ({
  default: (props: typeof oceanMock.props) => {
    oceanMock.props = props;
    return <div data-testid="ocean-scene" />;
  },
}));

function HomeFixture() {
  const { openAuth } = useOutletContext<PublicOutletContext>();
  return (
    <main>
      <h1>首页</h1>
      <Link to="/login" onClick={(event) => { event.preventDefault(); openAuth("login"); }}>登录</Link>
      <Link to="/register" onClick={(event) => { event.preventDefault(); openAuth("register"); }}>注册</Link>
    </main>
  );
}

function LocationFixture() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function LoginFixture({ historyButton = false }: { historyButton?: boolean }) {
  const navigate = useNavigate();
  return (
    <AuthLayout title="欢迎回来" subtitle="登录">
      <span>登录表单</span>
      <AuthTransitionLink target="register">注册</AuthTransitionLink>
      <AuthTransitionLink target="forgot-password" search="email=user%40example.com">忘记密码</AuthTransitionLink>
      {historyButton ? <button type="button" onClick={() => navigate(-1)}>浏览器后退</button> : null}
    </AuthLayout>
  );
}

function PublicRouteFixture({ historyButton = false }: { historyButton?: boolean }) {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route path="/" element={<><HomeFixture /><LocationFixture /></>} />
        <Route path="/login" element={<><LoginFixture historyButton={historyButton} /><LocationFixture /></>} />
        <Route path="/register" element={<><AuthLayout title="创建账号" subtitle="注册"><span>注册表单</span><AuthTransitionLink target="login">登录</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
        <Route path="/forgot-password" element={<><AuthLayout title="重设密码" subtitle="找回"><span>找回表单</span><AuthTransitionLink target="login">返回登录</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
        <Route path="/device/approve" element={<><AuthLayout title="设备授权" subtitle="设备"><span>设备表单</span><AuthTransitionLink target="login" search="next=%2Fdevice%2Fapprove%3Fgrant%3Dabc">前往登录</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
      </Route>
    </Routes>
  );
}

describe("PublicLayout", () => {
  beforeEach(() => {
    localStorage.clear();
    oceanMock.props = null;
    vi.useRealTimers();
  });

  it("defaults the switch on and keeps one ocean DOM node through home-to-auth emergence", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    const scene = screen.getByTestId("ocean-scene");
    expect(screen.getByRole("switch", { name: "认证特效" })).toBeChecked();
    fireEvent.click(screen.getByRole("link", { name: "登录" }));

    expect(screen.getByText("登录表单").closest(".auth-card")).toHaveClass("auth-card--emerge");
    expect(oceanMock.props?.transition?.phase).toBe("emerge");
    expect(container.querySelectorAll('[data-testid="ocean-scene"]')).toHaveLength(1);
    expect(screen.getByTestId("ocean-scene")).toBe(scene);
  });

  it("stores the local preference and uses immediate class-free navigation when disabled", () => {
    const first = render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("switch", { name: "认证特效" }));
    expect(localStorage.getItem(AUTH_EFFECTS_STORAGE_KEY)).toBe("false");
    fireEvent.click(screen.getByRole("link", { name: "登录" }));
    expect(screen.getByText("登录表单").closest(".auth-card")).not.toHaveClass("auth-card--emerge");
    expect(oceanMock.props?.transition).toBeNull();
    first.unmount();

    render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );
    expect(screen.getByRole("switch", { name: "认证特效" })).not.toBeChecked();
  });

  it("sinks the old card for 320ms before emerging the next authentication card", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "注册" }));
    expect(screen.getByText("登录表单").closest(".auth-card")).toHaveClass("auth-card--switch-out");
    expect(oceanMock.props?.transition?.phase).toBe("switch");

    act(() => vi.advanceTimersByTime(319));
    expect(screen.getByText("登录表单")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));

    expect(screen.getByText("注册表单").closest(".auth-card")).toHaveClass("auth-card--emerge");
    expect(oceanMock.props?.transition?.phase).toBe("emerge");
  });

  it("preserves query strings during animated authentication switches", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "忘记密码" }));
    act(() => vi.advanceTimersByTime(320));
    expect(screen.getByTestId("location")).toHaveTextContent("/forgot-password?email=user%40example.com");
  });

  it("animates device authorization to login and preserves its next target", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/device/approve?grant=abc"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "前往登录" }));
    expect(screen.getByText("设备表单").closest(".auth-card")).toHaveClass("auth-card--switch-out");
    act(() => vi.advanceTimersByTime(320));
    expect(screen.getByTestId("location")).toHaveTextContent("/login?next=%2Fdevice%2Fapprove%3Fgrant%3Dabc");
  });

  it("sinks for 900ms before an explicit return home", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "← 返回首页" }));
    expect(screen.getByText("登录表单").closest(".auth-card")).toHaveClass("auth-card--sink");
    act(() => vi.advanceTimersByTime(899));
    expect(screen.getByText("登录表单")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByRole("heading", { name: "首页" })).toBeInTheDocument();
  });

  it("finishes a pending navigation immediately when effects are turned off", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "注册" }));
    fireEvent.click(screen.getByRole("switch", { name: "认证特效" }));
    expect(screen.getByText("注册表单")).toBeInTheDocument();
    expect(screen.getByText("注册表单").closest(".auth-card")).not.toHaveClass("auth-card--emerge");
    expect(oceanMock.props?.transition).toBeNull();
  });

  it("uses a noninteractive sinking snapshot for browser POP back to home", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/", "/login"]} initialIndex={1}>
        <PublicRouteFixture historyButton />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "浏览器后退" }));
    expect(screen.getByRole("heading", { name: "首页" })).toBeInTheDocument();
    const snapshot = document.querySelector(".auth-exit-snapshot");
    expect(snapshot).toBeInTheDocument();
    expect(snapshot).toHaveAttribute("aria-hidden", "true");
    expect(snapshot?.querySelector(".auth-card")).toHaveClass("auth-card--sink");
    act(() => vi.advanceTimersByTime(900));
    expect(document.querySelector(".auth-exit-snapshot")).toBeNull();
  });

  it("does not animate direct authentication deep links", () => {
    render(
      <MemoryRouter initialEntries={["/register"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    expect(screen.getByText("注册表单").closest(".auth-card")).not.toHaveClass("auth-card--emerge");
    expect(oceanMock.props?.transition).toBeNull();
  });

  it("forces immediate behavior when the ocean reports a fallback", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );
    act(() => oceanMock.props?.onFallbackChange?.(true));
    expect(screen.getByRole("switch", { name: "认证特效" })).toBeDisabled();

    fireEvent.click(screen.getByRole("link", { name: "登录" }));
    expect(screen.getByText("登录表单").closest(".auth-card")).not.toHaveClass("auth-card--emerge");
  });
});
