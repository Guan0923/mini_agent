import { Switch, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Outlet, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import OceanScene, { type OceanTransition } from "../components/OceanScene";

export const AUTH_EFFECTS_STORAGE_KEY = "mini-agent-auth-effects-enabled";
export type AuthTarget = "login" | "register" | "forgot-password";
export type AuthRoute = AuthTarget | "device";

export interface AuthTransition {
  token: string;
  source: AuthRoute | "home";
  target: AuthRoute | "home";
  phase: "enter" | "switch-out" | "exit";
}
export interface AuthSnapshot {
  pathname: string;
  route: AuthRoute;
  title: string;
  subtitle: string;
}
export interface PublicOutletContext {
  authEffectsEnabled: boolean;
  setAuthEffectsEnabled: (enabled: boolean) => void;
  openAuth: (target: AuthTarget, options?: { search?: string }) => void;
  closeAuth: () => void;
  transition: AuthTransition | null;
  oceanFallback: boolean;
  registerAuthSnapshot: (snapshot: AuthSnapshot) => void;
}
interface ExitSnapshot extends AuthSnapshot {
  phase: "switch-out" | "exit";
}

const SWITCH_OUT_MS = 320;
const EMERGE_MS = 1050;
const EXIT_MS = 900;

function readEffectsPreference(): boolean {
  try {
    const stored = localStorage.getItem(AUTH_EFFECTS_STORAGE_KEY);
    return stored === null ? true : stored !== "false";
  } catch {
    return true;
  }
}

function routeForPath(pathname: string): AuthRoute | "home" | null {
  if (pathname === "/") return "home";
  if (pathname === "/login") return "login";
  if (pathname === "/register") return "register";
  if (pathname === "/forgot-password") return "forgot-password";
  if (pathname === "/device/approve") return "device";
  return null;
}

function targetPath(target: AuthTarget, search = ""): string {
  const normalizedSearch = search && !search.startsWith("?") ? `?${search}` : search;
  return `/${target}${normalizedSearch}`;
}

/** Keeps the public ocean scene mounted while foreground routes change. */
export default function PublicLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const navigationType = useNavigationType();
  const tokenRef = useRef(0);
  const timersRef = useRef(new Set<number>());
  const pendingNavigationRef = useRef<((animate?: boolean) => void) | null>(null);
  const snapshotsRef = useRef(new Map<string, AuthSnapshot>());
  const previousPathRef = useRef(location.pathname);
  const [effectsPreference, setEffectsPreference] = useState(readEffectsPreference);
  const [transition, setTransition] = useState<AuthTransition | null>(null);
  const [exitSnapshot, setExitSnapshot] = useState<ExitSnapshot | null>(null);
  const [oceanFallback, setOceanFallback] = useState(false);
  const authEffectsEnabled = effectsPreference && !oceanFallback;

  const clearTimers = useCallback(() => {
    timersRef.current.forEach((timer) => window.clearTimeout(timer));
    timersRef.current.clear();
  }, []);
  const schedule = useCallback((callback: () => void, delay: number) => {
    const timer = window.setTimeout(() => {
      timersRef.current.delete(timer);
      callback();
    }, delay);
    timersRef.current.add(timer);
  }, []);
  const cancelTransition = useCallback((completePending: boolean) => {
    clearTimers();
    const pendingNavigation = pendingNavigationRef.current;
    pendingNavigationRef.current = null;
    setTransition(null);
    setExitSnapshot(null);
    if (completePending) pendingNavigation?.(false);
  }, [clearTimers]);
  const registerAuthSnapshot = useCallback((snapshot: AuthSnapshot) => {
    snapshotsRef.current.set(snapshot.pathname, snapshot);
  }, []);
  const setAuthEffectsEnabled = useCallback((enabled: boolean) => {
    setEffectsPreference(enabled);
    try {
      localStorage.setItem(AUTH_EFFECTS_STORAGE_KEY, String(enabled));
    } catch {
      // Storage can be unavailable in private or hardened browser contexts.
    }
    if (!enabled) cancelTransition(true);
  }, [cancelTransition]);

  const openAuth = useCallback((target: AuthTarget, options?: { search?: string }) => {
    const destination = targetPath(target, options?.search);
    const source = routeForPath(location.pathname);
    if (!authEffectsEnabled || source === null) {
      cancelTransition(false);
      navigate(destination);
      return;
    }
    cancelTransition(false);
    const token = `${Date.now()}-${tokenRef.current++}`;
    if (source === "home") {
      setTransition({ token, source, target, phase: "enter" });
      navigate(destination);
      schedule(() => setTransition((current) => current?.token === token ? null : current), EMERGE_MS);
      return;
    }
    setTransition({ token, source, target, phase: "switch-out" });
    const finishNavigation = (animate = true) => {
      pendingNavigationRef.current = null;
      navigate(destination);
      if (!animate) {
        setTransition(null);
        return;
      }
      setTransition({ token, source, target, phase: "enter" });
      schedule(() => setTransition((current) => current?.token === token ? null : current), EMERGE_MS);
    };
    pendingNavigationRef.current = finishNavigation;
    schedule(finishNavigation, SWITCH_OUT_MS);
  }, [authEffectsEnabled, cancelTransition, location.pathname, navigate, schedule]);

  const closeAuth = useCallback(() => {
    const source = routeForPath(location.pathname);
    if (!authEffectsEnabled || source === null || source === "home") {
      cancelTransition(false);
      navigate("/", { replace: true });
      return;
    }
    cancelTransition(false);
    const token = `${Date.now()}-${tokenRef.current++}`;
    setTransition({ token, source, target: "home", phase: "exit" });
    const finishNavigation = () => {
      pendingNavigationRef.current = null;
      setTransition(null);
      navigate("/", { replace: true });
    };
    pendingNavigationRef.current = finishNavigation;
    schedule(finishNavigation, EXIT_MS);
  }, [authEffectsEnabled, cancelTransition, location.pathname, navigate, schedule]);

  useEffect(() => {
    if (oceanFallback) cancelTransition(true);
  }, [cancelTransition, oceanFallback]);

  useEffect(() => {
    const previousPath = previousPathRef.current;
    previousPathRef.current = location.pathname;
    if (navigationType !== "POP" || previousPath === location.pathname || !authEffectsEnabled) return;
    const source = routeForPath(previousPath);
    const target = routeForPath(location.pathname);
    if (!source || !target) return;
    cancelTransition(false);
    const token = `${Date.now()}-${tokenRef.current++}`;
    if (source !== "home") {
      const snapshot = snapshotsRef.current.get(previousPath);
      if (snapshot) {
        const phase = target === "home" ? "exit" : "switch-out";
        setExitSnapshot({ ...snapshot, phase });
        schedule(() => setExitSnapshot(null), phase === "exit" ? EXIT_MS : SWITCH_OUT_MS);
      }
    }
    if (target === "home") {
      setTransition({ token, source, target, phase: "exit" });
      schedule(() => setTransition((current) => current?.token === token ? null : current), EXIT_MS);
    } else {
      setTransition({ token, source, target, phase: "enter" });
      schedule(() => setTransition((current) => current?.token === token ? null : current), EMERGE_MS);
    }
  }, [authEffectsEnabled, cancelTransition, location.pathname, navigationType, schedule]);

  useEffect(() => () => clearTimers(), [clearTimers]);

  const oceanTransition = useMemo<OceanTransition | null>(() => {
    if (!authEffectsEnabled || !transition) return null;
    const phase = transition.phase === "enter" ? "emerge" : transition.phase === "exit" ? "sink" : "switch";
    return { token: `${transition.token}-${transition.phase}`, phase };
  }, [authEffectsEnabled, transition]);
  const outletContext = useMemo<PublicOutletContext>(() => ({
    authEffectsEnabled,
    setAuthEffectsEnabled,
    openAuth,
    closeAuth,
    transition,
    oceanFallback,
    registerAuthSnapshot,
  }), [authEffectsEnabled, closeAuth, oceanFallback, openAuth, registerAuthSnapshot, setAuthEffectsEnabled, transition]);


  return (
    <div className="public-shell">
      <OceanScene transition={oceanTransition} effectsEnabled={authEffectsEnabled} onFallbackChange={setOceanFallback} />
      <Outlet context={outletContext} />
      {exitSnapshot ? (
        <div className="auth-exit-snapshot" aria-hidden="true">
          <div className="auth-overlay">
            <div className="brand-mark auth-brand">MINI<span>·</span>AGENT</div>
            <div className={`auth-card auth-card--${exitSnapshot.phase === "exit" ? "sink" : "switch-out"}`}>
              <p className="eyebrow">MINI·AGENT</p>
              <h1>{exitSnapshot.title}</h1>
              <p className="auth-subtitle">{exitSnapshot.subtitle}</p>
            </div>
          </div>
        </div>
      ) : null}
      <Tooltip title={oceanFallback ? "当前设备或浏览器已启用静态海面，认证特效不可用。" : "控制登录与注册卡片的水花过渡。"}>
        <div className="auth-effects-toggle">
          <Switch size="small" checked={authEffectsEnabled} disabled={oceanFallback} onChange={setAuthEffectsEnabled} aria-label="认证特效" />
          <span>认证特效</span>
        </div>
      </Tooltip>
    </div>
  );
}