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

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("useSandboxHealth", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-04T00:00:00Z"));
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

  it("checks immediately and polls healthy or paused states every 30 seconds", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    render(<Harness />);
    await settle();

    expect(current.phase).toBe("healthy");
    expect(repairSandboxBroker).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(29_999));
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
  });

  it("repairs immediately after an unhealthy check and confirms health", async () => {
    vi.mocked(getSandboxStatus)
      .mockResolvedValueOnce({ installed: true, healthy: false, code: "broker_pipe_unavailable", detail: "stopped" })
      .mockResolvedValueOnce({ installed: true, healthy: true });
    render(<Harness />);
    await settle();

    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
    expect(current.nextRetryAt).toBeNull();
  });

  it("backs off at 1, 2, 4, 8, 16, and capped 30 second delays", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: false, detail: "stopped" });
    vi.mocked(repairSandboxBroker).mockRejectedValue(new ApiError(503, "repair failed", "broker_service_start_failed"));
    render(<Harness />);
    await settle();

    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    for (const [index, delay] of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000].entries()) {
      expect(current.autoRecoveryPhase).toBe("waiting");
      expect(current.nextRetryAt).toBe(Date.now() + delay);
      await act(async () => vi.advanceTimersByTimeAsync(delay - 1));
      expect(repairSandboxBroker).toHaveBeenCalledTimes(index + 1);
      await act(async () => vi.advanceTimersByTimeAsync(1));
      await settle();
      expect(repairSandboxBroker).toHaveBeenCalledTimes(index + 2);
    }
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
  });

  it("resets backoff after a successful repair", async () => {
    vi.mocked(getSandboxStatus)
      .mockResolvedValueOnce({ installed: true, healthy: false, detail: "stopped" })
      .mockResolvedValueOnce({ installed: true, healthy: true });
    vi.mocked(repairSandboxBroker)
      .mockRejectedValueOnce(new ApiError(503, "first", "broker_not_ready"))
      .mockResolvedValueOnce({ installed: true, healthy: true });
    render(<Harness />);
    await settle();
    expect(current.nextRetryAt).toBe(Date.now() + 1_000);

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    await settle();

    expect(repairSandboxBroker).toHaveBeenCalledTimes(2);
    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
    expect(current.nextRetryAt).toBeNull();
  });

  it("pauses only after UAC cancellation and keeps polling without repairing", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: false, detail: "stopped" });
    vi.mocked(repairSandboxBroker).mockRejectedValue(
      new ApiError(503, "安装已取消", "broker_uac_cancelled"),
    );
    render(<Harness />);
    await settle();

    expect(current.autoRecoveryPhase).toBe("paused");
    expect(current.code).toBe("broker_uac_cancelled");
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    await settle();
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    expect(current.autoRecoveryPhase).toBe("paused");
  });

  it("a user backend request wakes a paused recovery and resets delay to one second", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: false, detail: "stopped" });
    vi.mocked(repairSandboxBroker)
      .mockRejectedValueOnce(new ApiError(503, "安装已取消", "broker_uac_cancelled"))
      .mockRejectedValueOnce(new ApiError(503, "still stopped", "broker_service_start_failed"));
    render(<Harness />);
    await settle();
    expect(current.autoRecoveryPhase).toBe("paused");

    act(() => current.notifyUserBackendRequest());
    await settle();

    expect(repairSandboxBroker).toHaveBeenCalledTimes(2);
    expect(current.autoRecoveryPhase).toBe("waiting");
    expect(current.nextRetryAt).toBe(Date.now() + 1_000);
  });

  it("deduplicates checks and repair attempts", async () => {
    let resolveStatus!: (status: SandboxBrokerStatus) => void;
    let rejectRepair!: (cause: unknown) => void;
    vi.mocked(getSandboxStatus).mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));
    vi.mocked(repairSandboxBroker).mockReturnValue(new Promise((_resolve, reject) => { rejectRepair = reject; }));
    render(<Harness />);

    const first = current.check();
    const second = current.check();
    expect(first).toBe(second);
    expect(getSandboxStatus).toHaveBeenCalledTimes(1);
    await act(async () => resolveStatus({ installed: true, healthy: false, detail: "stopped" }));
    await settle();
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);

    act(() => {
      current.notifyUserBackendRequest();
      current.notifyUserBackendRequest();
    });
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
    await act(async () => rejectRepair(new ApiError(503, "failed", "broker_not_ready")));
  });

  it("runs a fresh health check after repair when an older check is still in flight", async () => {
    let resolveRepair!: (status: SandboxBrokerStatus) => void;
    let resolveStaleCheck!: (status: SandboxBrokerStatus) => void;
    vi.mocked(getSandboxStatus)
      .mockResolvedValueOnce({ installed: true, healthy: false, detail: "stopped" })
      .mockReturnValueOnce(new Promise((resolve) => { resolveStaleCheck = resolve; }))
      .mockResolvedValueOnce({ installed: true, healthy: true });
    vi.mocked(repairSandboxBroker).mockReturnValue(
      new Promise((resolve) => { resolveRepair = resolve; }),
    );
    render(<Harness />);
    await settle();
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);

    let staleCheck!: Promise<SandboxBrokerStatus>;
    act(() => {
      staleCheck = current.check();
    });
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    await act(async () => resolveRepair({ installed: true, healthy: true }));
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);

    await act(async () => resolveStaleCheck({ installed: true, healthy: false, detail: "stale" }));
    await staleCheck;
    await settle();

    expect(getSandboxStatus).toHaveBeenCalledTimes(3);
    expect(current.phase).toBe("healthy");
    expect(current.autoRecoveryPhase).toBe("idle");
  });

  it("clears a scheduled retry when unmounted", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: false, detail: "stopped" });
    vi.mocked(repairSandboxBroker).mockRejectedValue(new ApiError(503, "failed", "broker_not_ready"));
    const view = render(<Harness />);
    await settle();
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);

    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(repairSandboxBroker).toHaveBeenCalledTimes(1);
  });

  it("keeps confirmed reinstall available and immediately rechecks", async () => {
    vi.mocked(getSandboxStatus).mockResolvedValue({ installed: true, healthy: true });
    render(<Harness />);
    await settle();

    await act(async () => current.reinstall());

    expect(reinstallSandboxBroker).toHaveBeenCalledTimes(1);
    expect(getSandboxStatus).toHaveBeenCalledTimes(2);
    expect(current.phase).toBe("healthy");
    expect(current.reinstalling).toBe(false);

    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(getSandboxStatus).toHaveBeenCalledTimes(3);
  });
});
