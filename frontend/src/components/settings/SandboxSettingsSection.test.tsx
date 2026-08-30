import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UserSettingsState } from "./useUserSettingsState";
import { brokerErrorTitle, SandboxSettingsSection } from "./SandboxSettingsSection";

describe("brokerErrorTitle", () => {
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

  it("uses the generic installed fallback for an unknown code", () => {
    expect(brokerErrorTitle("future_broker_failure", true)).toBe("沙箱 Broker 异常");
  });

  it("uses the missing fallback for an old response without a code", () => {
    expect(brokerErrorTitle(null, false)).toBe("沙箱 Broker 未安装");
  });

  it.each(["healthy", "unhealthy"] as const)("always exposes confirmed reinstall while %s", async (phase) => {
    const reinstall = vi.fn().mockResolvedValue(undefined);
    const state = {
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
        repairing: false,
        reinstalling: false,
        check: vi.fn(),
        repair: vi.fn(),
        reinstall,
      },
      updateSettings: vi.fn(),
    } as unknown as UserSettingsState;

    render(<SandboxSettingsSection state={state} />);
    fireEvent.click(screen.getByRole("button", { name: "卸载并重装" }));
    expect(await screen.findByText("卸载并重装 Sandbox Broker？")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(reinstall).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "卸载并重装" }));
    expect(await screen.findByText("卸载并重装 Sandbox Broker？")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "卸载并重装" });
    fireEvent.click(buttons[buttons.length - 1]);
    expect(reinstall).toHaveBeenCalledTimes(1);
  });
});
