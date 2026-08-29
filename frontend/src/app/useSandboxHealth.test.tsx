import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  getSandboxStatus,
  reinstallSandboxBroker,
  repairSandboxBroker,
  type SandboxBrokerStatus,
} from "../api";
import { useSandboxHealth, type SandboxHealthState } from "./useSandboxHealth";

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  getSandboxStatus: vi.fn(),
  repairSandboxBroker: vi.fn(),
  reinstallSandboxBroker: vi.fn(),
}));

let current: SandboxHealthState;

function Harness() {
  current = useSandboxHealth();
  return <output>{current.phase}</output>;
}

describe("useSandboxHealth", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(getSandboxStatus).mockReset();
    vi.mocked(repairSandboxBroker).mockReset();
    vi.mocked(reinstallSandboxBroker).mockReset();
    vi.mocked(repairSandboxBroker).mockResolvedValue({ installed: true, healthy: true });
    vi.mocked(reinstallSandboxBroker).mockResolvedValue({ installed: true, healthy: true });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("checks immediately and waits 30 seconds after completion before polling again", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    render(<Harness />);

    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await act(async () => Promise.resolve());
    expect(current.phase).toBe("healthy");

    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
  });

  it("deduplicates startup, manual, and polling checks", async () => {
    let resolveStatus!: (status: SandboxBrokerStatus) => void;
    vi.mocked(getSandboxStatus).mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));
    render(<Harness />);

    let first!: Promise<SandboxBrokerStatus>;
    let second!: Promise<SandboxBrokerStatus>;
    act(() => {
      first = current.check();
      second = current.check();
    });
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    expect(first).toBe(second);

    await act(async () => {
      resolveStatus({ installed: true, healthy: true });
      await Promise.all([first, second]);
    });
    expect(current.phase).toBe("healthy");
  });

  it("waits for an active check, repairs, then immediately rechecks", async () => {
    let resolveInitial!: (status: SandboxBrokerStatus) => void;
    vi.mocked(getSandboxStatus)
      .mockReturnValueOnce(new Promise((resolve) => { resolveInitial = resolve; }))
      .mockResolvedValueOnce({ installed: true, healthy: true });
    render(<Harness />);

    let repair!: Promise<void>;
    act(() => {
      repair = current.repair();
    });
    expect(repairSandboxBroker).not.toHaveBeenCalled();

    await act(async () => {
      resolveInitial({ installed: true, healthy: false, detail: "service stopped" });
      await repair;
    });

    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    expect(current.phase).toBe("healthy");
    expect(current.repairing).toBe(false);
  });

  it("preserves the status code and complete raw detail", async () => {
    const detail = "  Broker service configuration requires repair\nSCM detail\n";
    vi.mocked(getSandboxStatus).mockResolvedValue({
      installed: true,
      healthy: false,
      code: "broker_service_configuration_invalid",
      detail,
    });

    render(<Harness />);
    await act(async () => Promise.resolve());

    expect(current.phase).toBe("unhealthy");
    expect(current.installed).toBe(true);
    expect(current.code).toBe("broker_service_configuration_invalid");
    expect(current.detail).toBe(detail);
  });

  it("preserves the repair failure code and complete raw detail", async () => {
    const detail = "  Broker Windows 服务启动失败。\nSCM 7000\n";
    vi.mocked(getSandboxStatus).mockResolvedValue({
      installed: true,
      healthy: false,
      code: "broker_pipe_unavailable",
      detail: "Windows Broker pipe is unavailable",
    });
    vi.mocked(repairSandboxBroker).mockRejectedValue(
      new ApiError(503, detail, "broker_service_start_failed"),
    );
    render(<Harness />);
    await act(async () => Promise.resolve());

    await act(async () => current.repair());

    expect(current.phase).toBe("unhealthy");
    expect(current.code).toBe("broker_service_start_failed");
    expect(current.detail).toBe(detail);
    expect(current.repairing).toBe(false);
  });

  it("forces reinstall while healthy and immediately rechecks", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    render(<Harness />);
    await act(async () => Promise.resolve());

    await act(async () => current.reinstall());

    expect(reinstallSandboxBroker).toHaveBeenCalledTimes(1);
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    expect(current.phase).toBe("healthy");
    expect(current.reinstalling).toBe(false);
  });

  it("preserves an active-job reinstall conflict", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    vi.mocked(reinstallSandboxBroker).mockRejectedValue(
      new ApiError(409, "仍有沙箱命令正在运行。", "broker_jobs_active"),
    );
    render(<Harness />);
    await act(async () => Promise.resolve());

    await act(async () => current.reinstall());

    expect(current.phase).toBe("unhealthy");
    expect(current.code).toBe("broker_jobs_active");
    expect(current.detail).toBe("仍有沙箱命令正在运行。");
  });

  it("preserves a categorized reinstall failure", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    vi.mocked(reinstallSandboxBroker).mockRejectedValue(
      new ApiError(503, "Broker Windows 服务启动失败。", "broker_service_start_failed"),
    );
    render(<Harness />);
    await act(async () => Promise.resolve());

    await act(async () => current.reinstall());

    expect(current.phase).toBe("unhealthy");
    expect(current.code).toBe("broker_service_start_failed");
    expect(current.detail).toBe("Broker Windows 服务启动失败。");
    expect(current.reinstalling).toBe(false);
  });
});
