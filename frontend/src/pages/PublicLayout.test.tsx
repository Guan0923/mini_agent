import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { BeachEnvironment } from "../components/OceanScene";
import PublicLayout from "./PublicLayout";
import AuthLayout, { AuthTransitionLink } from "./auth/AuthLayout";

const oceanMock = vi.hoisted(() => ({
  props: null as null | { environment?: BeachEnvironment },
}));

vi.mock("../components/OceanScene", () => ({
  default: (props: typeof oceanMock.props) => {
    oceanMock.props = props;
    return <div data-testid="ocean-scene" />;
  },
}));

function HomeFixture() {
  return (
    <main>
      <h1>首页</h1>
      <Link to="/login">登录</Link>
      <Link to="/register">注册</Link>
    </main>
  );
}

function LocationFixture() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function PublicRouteFixture() {
  return (
    <Routes>
      <Route element={<PublicLayout />}>
        <Route path="/" element={<><HomeFixture /><LocationFixture /></>} />
        <Route path="/login" element={<><AuthLayout title="欢迎回来" subtitle="登录"><span>登录表单</span><AuthTransitionLink target="register">注册</AuthTransitionLink><AuthTransitionLink target="forgot-password" search="email=user%40example.com">忘记密码</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
        <Route path="/register" element={<><AuthLayout title="创建账号" subtitle="注册"><span>注册表单</span><AuthTransitionLink target="login">登录</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
        <Route path="/forgot-password" element={<><AuthLayout title="重设密码" subtitle="找回"><span>找回表单</span><AuthTransitionLink target="login">返回登录</AuthTransitionLink></AuthLayout><LocationFixture /></>} />
      </Route>
    </Routes>
  );
}

describe("PublicLayout", () => {
  beforeEach(() => {
    oceanMock.props = null;
    Object.defineProperty(navigator, "geolocation", { configurable: true, value: undefined });
  });

  it("navigates immediately and keeps one ocean scene mounted", () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    const scene = screen.getByTestId("ocean-scene");
    expect(screen.queryByRole("switch", { name: "认证特效" })).toBeNull();
    fireEvent.click(screen.getByRole("link", { name: "登录" }));

    expect(screen.getByText("登录表单").closest(".auth-card")).toHaveClass("auth-card--staggered-reveal");
    expect(container.querySelectorAll('[data-testid="ocean-scene"]')).toHaveLength(1);
    expect(screen.getByTestId("ocean-scene")).toBe(scene);
    expect(document.querySelector(".auth-exit-snapshot")).toBeNull();
  });

  it("switches authentication routes immediately and preserves query strings", () => {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "注册" }));
    expect(screen.getByText("注册表单")).toBeInTheDocument();
    expect(screen.queryByText("登录表单")).toBeNull();

    fireEvent.click(screen.getByRole("link", { name: "登录" }));
    fireEvent.click(screen.getByRole("link", { name: "忘记密码" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/forgot-password?email=user%40example.com");
  });

  it("returns home immediately without leaving an exit snapshot", () => {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "← 返回首页" }));
    expect(screen.getByRole("heading", { name: "首页" })).toBeInTheDocument();
    expect(document.querySelector(".auth-exit-snapshot")).toBeNull();
  });

  it("keeps location light off until the user explicitly grants a position", () => {
    const getCurrentPosition = vi.fn((success: PositionCallback) => {
      success({ coords: { latitude: 31.23, longitude: 121.47 } as GeolocationCoordinates } as GeolocationPosition);
    });
    Object.defineProperty(navigator, "geolocation", { configurable: true, value: { getCurrentPosition } });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    const locationButton = screen.getByRole("button", { name: "位置光照" });
    expect(locationButton).toHaveAttribute("aria-pressed", "false");
    expect(getCurrentPosition).not.toHaveBeenCalled();
    expect(oceanMock.props?.environment).toMatchObject({ locationEnabled: false, timeZone: "Asia/Shanghai", coordinates: null });

    fireEvent.click(locationButton);
    expect(getCurrentPosition).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "已启用位置光照" })).toHaveAttribute("aria-pressed", "true");
    expect(oceanMock.props?.environment?.coordinates).toEqual({ latitude: 31.23, longitude: 121.47 });

    fireEvent.click(screen.getByRole("link", { name: "注册" }));
    expect(screen.getByText("注册表单")).toBeInTheDocument();
    expect(oceanMock.props?.environment?.locationEnabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "已启用位置光照" }));
    expect(oceanMock.props?.environment).toMatchObject({ locationEnabled: false, timeZone: "Asia/Shanghai", coordinates: null });
  });

  it("falls back to Beijing time when location permission is denied", () => {
    const getCurrentPosition = vi.fn((_success: PositionCallback, error?: PositionErrorCallback) => {
      error?.({ code: 1, message: "denied" } as GeolocationPositionError);
    });
    Object.defineProperty(navigator, "geolocation", { configurable: true, value: { getCurrentPosition } });

    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "位置光照" }));
    expect(screen.getByText("定位权限被拒绝，已使用北京时间。")).toBeInTheDocument();
    expect(oceanMock.props?.environment).toMatchObject({ locationEnabled: false, timeZone: "Asia/Shanghai", coordinates: null });
  });

  it("shows a readable fallback when geolocation is unavailable", () => {
    render(
      <MemoryRouter initialEntries={["/login"]}>
        <PublicRouteFixture />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "位置光照" }));
    expect(screen.getByText("当前浏览器不支持定位，已使用北京时间。")).toBeInTheDocument();
    expect(oceanMock.props?.environment).toMatchObject({ locationEnabled: false, timeZone: "Asia/Shanghai", coordinates: null });
  });
});
