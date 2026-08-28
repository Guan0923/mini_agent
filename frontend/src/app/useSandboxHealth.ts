import { useCallback, useEffect, useRef, useState } from "react";
import { getSandboxStatus, repairSandboxBroker, type SandboxBrokerStatus } from "../api";

export type SandboxHealthPhase = "checking" | "healthy" | "unhealthy";

export interface SandboxHealthState {
  phase: SandboxHealthPhase;
  installed: boolean;
  detail: string | null;
  checking: boolean;
  repairing: boolean;
  check: () => Promise<SandboxBrokerStatus>;
  repair: () => Promise<void>;
}

const POLL_DELAY_MS = 30_000;

function failedStatus(cause: unknown): SandboxBrokerStatus {
  return {
    installed: false,
    healthy: false,
    detail: cause instanceof Error ? cause.message : "无法连接沙箱 Broker。",
  };
}

export function useSandboxHealth(): SandboxHealthState {
  const [phase, setPhase] = useState<SandboxHealthPhase>("checking");
  const [installed, setInstalled] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [repairing, setRepairing] = useState(false);
  const mountedRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof globalThis.setTimeout> | null>(null);
  const checkInFlightRef = useRef<Promise<SandboxBrokerStatus> | null>(null);
  const repairInFlightRef = useRef<Promise<void> | null>(null);
  const checkRef = useRef<() => Promise<SandboxBrokerStatus>>(() => Promise.resolve(failedStatus(null)));

  const scheduleNext = useCallback(() => {
    if (!mountedRef.current) return;
    if (timerRef.current !== null) globalThis.clearTimeout(timerRef.current);
    timerRef.current = globalThis.setTimeout(() => {
      timerRef.current = null;
      void checkRef.current();
    }, POLL_DELAY_MS);
  }, []);

  const check = useCallback((): Promise<SandboxBrokerStatus> => {
    if (checkInFlightRef.current) return checkInFlightRef.current;
    if (timerRef.current !== null) {
      globalThis.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (mountedRef.current) {
      setPhase("checking");
      setChecking(true);
    }
    const request = getSandboxStatus()
      .catch((cause) => failedStatus(cause))
      .then((status) => {
        if (mountedRef.current) {
          const healthy = status.installed && status.healthy;
          setInstalled(status.installed);
          setDetail(healthy ? null : status.detail?.trim() || (
            status.installed ? "沙箱 Broker 健康检查未通过。" : "沙箱 Broker 未安装。"
          ));
          setPhase(healthy ? "healthy" : "unhealthy");
        }
        return status;
      })
      .finally(() => {
        checkInFlightRef.current = null;
        if (mountedRef.current) {
          setChecking(false);
          scheduleNext();
        }
      });
    checkInFlightRef.current = request;
    return request;
  }, [scheduleNext]);
  checkRef.current = check;

  const repair = useCallback((): Promise<void> => {
    if (repairInFlightRef.current) return repairInFlightRef.current;
    const request = (async () => {
      if (checkInFlightRef.current) await checkInFlightRef.current;
      if (!mountedRef.current) return;
      setRepairing(true);
      if (timerRef.current !== null) {
        globalThis.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      try {
        await repairSandboxBroker();
        await check();
      } catch (cause) {
        if (!mountedRef.current) return;
        setDetail(cause instanceof Error ? cause.message : "沙箱 Broker 修复失败。");
        setPhase("unhealthy");
        scheduleNext();
      } finally {
        if (mountedRef.current) setRepairing(false);
      }
    })().finally(() => {
      repairInFlightRef.current = null;
    });
    repairInFlightRef.current = request;
    return request;
  }, [check, scheduleNext]);

  useEffect(() => {
    mountedRef.current = true;
    void check();
    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) globalThis.clearTimeout(timerRef.current);
    };
  }, [check]);

  return { phase, installed, detail, checking, repairing, check, repair };
}
