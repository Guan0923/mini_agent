import { describe, expect, it } from "vitest";
import { brokerErrorTitle } from "./SandboxSettingsSection";

describe("brokerErrorTitle", () => {
  it.each([
    ["broker_not_installed", "沙箱 Broker 未安装"],
    ["broker_service_configuration_invalid", "Broker 服务配置异常"],
    ["broker_ready_marker_unavailable", "Broker 就绪信息缺失"],
    ["broker_pipe_unavailable", "Broker 通信失败"],
    ["broker_protocol_incompatible", "Broker 协议版本不兼容"],
    ["broker_service_start_failed", "Broker Windows 服务启动失败"],
    ["broker_admin_required", "Broker 修复需要管理员权限"],
  ])("maps %s to a distinct title", (code, title) => {
    expect(brokerErrorTitle(code, true)).toBe(title);
  });

  it("uses the generic installed fallback for an unknown code", () => {
    expect(brokerErrorTitle("future_broker_failure", true)).toBe("沙箱 Broker 异常");
  });

  it("uses the missing fallback for an old response without a code", () => {
    expect(brokerErrorTitle(null, false)).toBe("沙箱 Broker 未安装");
  });
});
