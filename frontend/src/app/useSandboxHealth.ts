import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  getSandboxStatus,
  reinstallSandboxBroker,
  repairSandboxBroker,
  type SandboxBrokerStatus,
} from "../api";

export type SandboxHealthPhase = "checking" | "healthy" | "unhealthy";
export type SandboxAutoRecoveryPhase = "idle" | "repairing" | "waiting" | "paused";

export interface SandboxHealthState {
  phase: SandboxHealthPhase;
  installed: boolean;
  code: string | null;
  detail: string | null;
  checking: boolean;
  autoRecoveryPhase: SandboxAutoRecoveryPhase;
  nextRetryAt: number | null;
  reinstalling: boolean;
  check: () => Promise<SandboxBrokerStatus>;
  notifyUserBackendRequest: () => void;
  reinstall: () => Promise<void>;
}

const POLL_DELAY_MS = 30_000;
const INITIAL_RETRY_DELAY_MS = 1_000;
const MAX_RETRY_DELAY_MS = 30_000;
const PAUSED_REPAIR_CODE = "broker_uac_cancelled";

function failedStatus(cause: unknown): SandboxBrokerStatus {
  return {
    installed: false,
    healthy: false,
    code: cause instanceof ApiError ? cause.code ?? "broker_status_failed" : "broker_status_failed",
    detail: cause instanceof Error ? cause.message : "无法连接沙箱 Broker。",
  };
}

function statusIsHealthy(status: SandboxBrokerStatus): boolean {
  return status.installed && status.healthy;
}

export function useSandboxHealth(): SandboxHealthState {
  const [phase, setPhase] = useState<SandboxHealthPhase>("checking");
  const [installed, setInstalled] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [autoRecoveryPhase, setAutoRecoveryPhase] = useState<SandboxAutoRecoveryPhase>("idle");
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);
  const [reinstalling, setReinstalling] = useState(false);
  const mountedRef = useRef(false);
  const phaseRef = useRef<SandboxHealthPhase>("checking");
  const autoRecoveryPhaseRef = useRef<SandboxAutoRecoveryPhase>("idle");
  const retryAttemptRef = useRef(0);
  const reinstallingRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);
  const checkInFlightRef = useRef<Promise<SandboxBrokerStatus> | null>(null);
  const repairInFlightRef = useRef<Promise<void> | null>(null);
  const reinstallInFlightRef = useRef<Promise<void> | null>(null);
  const checkRef = useRef<() => Promise<SandboxBrokerStatus>>(() => Promise.resolve(failedStatus(null)));
  const repairRef = useRef<() => Promise<void>>(() => Promise.resolve());

  const clearScheduled = useCallback(() => {
    if (timerRef.current !== null) {
      globalThis.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (mountedRef.current) setNextRetryAt(null);
  }, []);

  const updateAutoRecoveryPhase = useCallback((next: SandboxAutoRecoveryPhase) => {
    autoRecoveryPhaseRef.current = next;
    if (mountedRef.current) setAutoRecoveryPhase(next);
  }, []);

  const resetAutoRecovery = useCallback(() => {
    retryAttemptRef.current = 0;
    clearScheduled();
    updateAutoRecoveryPhase("idle");
  }, [clearScheduled, updateAutoRecoveryPhase]);

  const applyStatus = useCallback((status: SandboxBrokerStatus): boolean => {
    const healthy = statusIsHealthy(status);
    phaseRef.current = healthy ? "healthy" : "unhealthy";
    if (mountedRef.current) {
      setInstalled(status.installed);
      setCode(healthy ? null : status.code?.trim() || (
        status.installed ? "broker_unhealthy" : "broker_not_installed"
      ));
      setDetail(healthy ? null : status.detail || (
        status.installed ? "沙箱 Broker 健康检查未通过。" : "沙箱 Broker 未安装。"
      ));
      setPhase(healthy ? "healthy" : "unhealthy");
    }
    return healthy;
  }, []);

  const applyRepairFailure = useCallback((cause: unknown): string => {
    const failureCode = cause instanceof ApiError ? cause.code ?? "broker_install_failed" : "broker_install_failed";
    phaseRef.current = "unhealthy";
    if (mountedRef.current) {
      setCode(failureCode);
      setDetail(cause instanceof Error ? cause.message : "沙箱 Broker 修复失败。");
      setPhase("unhealthy");
    }
    return failureCode;
  }, []);

  const schedulePoll = useCallback(() => {
    if (!mountedRef.current || autoRecoveryPhaseRef.current === "waiting" || reinstallingRef.current) return;
    clearScheduled();
    timerRef.current = globalThis.setTimeout(() => {
      timerRef.current = null;
      void checkRef.current();
    }, POLL_DELAY_MS);
  }, [clearScheduled]);

  const scheduleRetry = useCallback(() => {
    if (!mountedRef.current || reinstallingRef.current) return;
    clearScheduled();
    const delay = Math.min(
      MAX_RETRY_DELAY_MS,
      INITIAL_RETRY_DELAY_MS * (2 ** retryAttemptRef.current),
    );
    retryAttemptRef.current += 1;
    updateAutoRecoveryPhase("waiting");
    setNextRetryAt(Date.now() + delay);
    timerRef.current = globalThis.setTimeout(() => {
      timerRef.current = null;
      setNextRetryAt(null);
      void repairRef.current();
    }, delay);
  }, [clearScheduled, updateAutoRecoveryPhase]);

  const check = useCallback((): Promise<SandboxBrokerStatus> => {
    if (checkInFlightRef.current) return checkInFlightRef.current;
    clearScheduled();
    if (mountedRef.current) {
      phaseRef.current = "checking";
      setPhase("checking");
      setChecking(true);
    }
    let healthy = false;
    const request = getSandboxStatus()
      .catch((cause) => failedStatus(cause))
      .then((status) => {
        healthy = applyStatus(status);
        if (healthy) resetAutoRecovery();
        return status;
      })
      .finally(() => {
        checkInFlightRef.current = null;
        if (!mountedRef.current) return;
        setChecking(false);
        if (healthy || autoRecoveryPhaseRef.current === "paused") {
          schedulePoll();
        } else if (autoRecoveryPhaseRef.current !== "repairing" && !reinstallingRef.current) {
          void repairRef.current();
        }
      });
    checkInFlightRef.current = request;
    return request;
  }, [applyStatus, clearScheduled, resetAutoRecovery, schedulePoll]);
  checkRef.current = check;

  const repair = useCallback((): Promise<void> => {
    if (repairInFlightRef.current) return repairInFlightRef.current;
    if (
      !mountedRef.current
      || reinstallingRef.current
      || phaseRef.current === "healthy"
      || autoRecoveryPhaseRef.current === "paused"
    ) {
      return Promise.resolve();
    }
    const request = (async () => {
      if (checkInFlightRef.current) await checkInFlightRef.current;
      if (
        !mountedRef.current
        || reinstallingRef.current
        || phaseRef.current === "healthy"
        || autoRecoveryPhaseRef.current === "paused"
      ) return;
      clearScheduled();
      updateAutoRecoveryPhase("repairing");
      try {
        await repairSandboxBroker();
        if (!mountedRef.current) return;
        if (checkInFlightRef.current) await checkInFlightRef.current;
        if (!mountedRef.current) return;
        const status = await check();
        if (!statusIsHealthy(status)) scheduleRetry();
      } catch (cause) {
        if (!mountedRef.current) return;
        const failureCode = applyRepairFailure(cause);
        if (failureCode === PAUSED_REPAIR_CODE) {
          clearScheduled();
          updateAutoRecoveryPhase("paused");
          schedulePoll();
        } else {
          scheduleRetry();
        }
      }
    })().finally(() => {
      repairInFlightRef.current = null;
    });
    repairInFlightRef.current = request;
    return request;
  }, [applyRepairFailure, check, clearScheduled, schedulePoll, scheduleRetry, updateAutoRecoveryPhase]);
  repairRef.current = repair;

  const notifyUserBackendRequest = useCallback(() => {
    if (!mountedRef.current || reinstallingRef.current) return;
    if (phaseRef.current !== "unhealthy" && autoRecoveryPhaseRef.current === "idle") return;
    retryAttemptRef.current = 0;
    clearScheduled();
    if (autoRecoveryPhaseRef.current !== "repairing") updateAutoRecoveryPhase("idle");
    void repairRef.current();
  }, [clearScheduled, updateAutoRecoveryPhase]);

  const reinstall = useCallback((): Promise<void> => {
    if (reinstallInFlightRef.current) return reinstallInFlightRef.current;
    reinstallingRef.current = true;
    retryAttemptRef.current = 0;
    clearScheduled();
    updateAutoRecoveryPhase("idle");
    if (mountedRef.current) setReinstalling(true);
    const request = (async () => {
      if (repairInFlightRef.current) await repairInFlightRef.current;
      if (checkInFlightRef.current) await checkInFlightRef.current;
      if (!mountedRef.current) return;
      let shouldResumeAutoRecovery = false;
      try {
        await reinstallSandboxBroker();
        if (!mountedRef.current) return;
        if (checkInFlightRef.current) await checkInFlightRef.current;
        if (!mountedRef.current) return;
        const status = await check();
        shouldResumeAutoRecovery = !statusIsHealthy(status);
      } catch (cause) {
        if (!mountedRef.current) return;
        const failureCode = applyRepairFailure(cause);
        if (failureCode === PAUSED_REPAIR_CODE) {
          updateAutoRecoveryPhase("paused");
        } else {
          shouldResumeAutoRecovery = true;
        }
      } finally {
        reinstallingRef.current = false;
        if (mountedRef.current) setReinstalling(false);
      }
      if (autoRecoveryPhaseRef.current === "paused") schedulePoll();
      else if (shouldResumeAutoRecovery) void repairRef.current();
      else schedulePoll();
    })().finally(() => {
      reinstallInFlightRef.current = null;
    });
    reinstallInFlightRef.current = request;
    return request;
  }, [applyRepairFailure, check, clearScheduled, schedulePoll, updateAutoRecoveryPhase]);

  useEffect(() => {
    mountedRef.current = true;
    void check();
    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) {
        globalThis.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [check]);

  return {
    phase,
    installed,
    code,
    detail,
    checking,
    autoRecoveryPhase,
    nextRetryAt,
    reinstalling,
    check,
    notifyUserBackendRequest,
    reinstall,
  };
}
