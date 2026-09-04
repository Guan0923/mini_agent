import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SandboxAutoRecoveryPhase } from "../../app/useSandboxHealth";
import type { UserSettingsState } from "./useUserSettingsState";
import { brokerErrorTitle, SandboxSettingsSection } from "./SandboxSettingsSection";

function makeState(
  phase: "healthy" | "unhealthy" = "healthy",
  autoRecoveryPhase: SandboxAutoRecoveryPhase = "idle",
  nextRetryAt: number | null = null,
): UserSettingsState {
  return {
    settings: {
      sandbox_config: {
        network_mode: "no_network",
        network_allowlist: [],
        proxy_port: 17831,
        limits: {
          wall_seconds: 30,
          cpu_seconds: 30,
          memory_mib: 512,
          processes: 16,
          handles: 1024,
          output_chars: 20000,
          write_io_mib: 0,
        },
      },
    },
    sandboxHealth: {
      phase,
      installed: true,
      code: phase === "healthy" ? null : "broker_unhealthy",
      detail: phase === "healthy" ? null : "unhealthy",
      checking: false,
      autoRecoveryPhase,
      nextRetryAt,
      reinstalling: false,
      check: vi.fn().mockResolvedValue({ installed: true, healthy: phase === "healthy" }),
      notifyUserBackendRequest: vi.fn(),
      reinstall: vi.fn().mockResolvedValue(undefined),
    },
    updateSettings: vi.fn(),
  } as unknown as UserSettingsState;
}

describe("brokerErrorTitle", () => {
  afterEach(() => cleanup());

  it.each([
    ["broker_not_installed", "沙箱 Broker 未安装"],
    ["broker_service_configuration_invalid", "Broker 服务配置异常"],
    ["broker_ready_marker_unavailable", "Broker 就绪信息缺失"],
    ["broker_pipe_unavailable", "Broker 通信失败"],
    ["broker_protocol_incompatible", "Broker 协议版本不兼容"],
    ["broker_service_start_failed", "Broker Windows 服务启动失败"],
    ["broker_admin_required", "Broker 修复需要管理员权限"],
    ["broker_jobs_active", "仍有沙箱命令运行"],
  ])("maps %s to a distinct title", (code, title) => {
    expect(brokerErrorTitle(code, true)).toBe(title);
  });

  it("uses stable fallbacks for unknown and missing codes", () => {
    expect(brokerErrorTitle("future_broker_failure", true)).toBe("沙箱 Broker 异常");
    expect(brokerErrorTitle(null, false)).toBe("沙箱 Broker 未安装");
  });

  it("removes the repair button and shows automatic recovery states", () => {
    const { rerender } = render(<SandboxSettingsSection state={makeState("unhealthy", "repairing")} />);
    expect(screen.queryByRole("button", { name: "修复" })).not.toBeInTheDocument();
    expect(screen.getByText("正在自动修复")).toBeInTheDocument();

    rerender(<SandboxSettingsSection state={makeState("unhealthy", "waiting", Date.now() + 5_000)} />);
    expect(screen.getByText("5 秒后自动重试")).toBeInTheDocument();

    rerender(<SandboxSettingsSection state={makeState("unhealthy", "paused")} />);
    expect(screen.getByText("自动修复已暂停")).toBeInTheDocument();
  });

  it("treats a manual check as user activity before checking", () => {
    const state = makeState("unhealthy", "paused");
    render(<SandboxSettingsSection state={state} />);
    fireEvent.click(screen.getByRole("button", { name: /检\s*查/ }));

    expect(state.sandboxHealth.notifyUserBackendRequest).toHaveBeenCalledTimes(1);
    expect(state.sandboxHealth.check).toHaveBeenCalledTimes(1);
  });

  it.each(["healthy", "unhealthy"] as const)("always exposes confirmed reinstall while %s", async (phase) => {
    const state = makeState(phase);
    render(<SandboxSettingsSection state={state} />);
    fireEvent.click(screen.getByRole("button", { name: "卸载并重装" }));
    expect(await screen.findByText("卸载并重装 Sandbox Broker？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(state.sandboxHealth.reinstall).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "卸载并重装" }));
    const buttons = screen.getAllByRole("button", { name: "卸载并重装" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(state.sandboxHealth.reinstall).toHaveBeenCalledTimes(1);
  });
});
