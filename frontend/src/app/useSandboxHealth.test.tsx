import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getSandboxStatus, repairSandboxBroker, type SandboxBrokerStatus } from "../api";
import { useSandboxHealth, type SandboxHealthState } from "./useSandboxHealth";

vi.mock("../api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../api")>(),
  getSandboxStatus: vi.fn(),
  repairSandboxBroker: vi.fn(),
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
    vi.mocked(repairSandboxBroker).mockResolvedValue({ installed: true, healthy: true });
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
});
