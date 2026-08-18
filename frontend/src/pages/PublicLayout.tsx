import { Switch, Tooltip } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Outlet, useLocation, useNavigate, useNavigationType } from "react-router-dom";
import OceanScene, { type BeachEnvironment, type OceanTransition } from "../components/OceanScene";
import { DEFAULT_TIME_ZONE } from "../components/beach/sunPosition";

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

const DEFAULT_BEACH_ENVIRONMENT: BeachEnvironment = {
  locationEnabled: false,
  timeZone: DEFAULT_TIME_ZONE,
  coordinates: null,
};

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
  const [beachEnvironment, setBeachEnvironment] = useState<BeachEnvironment>(DEFAULT_BEACH_ENVIRONMENT);
  const [locationStatus, setLocationStatus] = useState<"idle" | "requesting" | "enabled" | "error">("idle");
  const [locationError, setLocationError] = useState("");
  const locationMessageTimerRef = useRef<number | null>(null);
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

  const resetBeachLocation = useCallback(() => {
    if (locationMessageTimerRef.current !== null) {
      window.clearTimeout(locationMessageTimerRef.current);
      locationMessageTimerRef.current = null;
    }
    setBeachEnvironment(DEFAULT_BEACH_ENVIRONMENT);
    setLocationStatus("idle");
    setLocationError("");
  }, []);

  const showLocationError = useCallback((message: string) => {
    if (locationMessageTimerRef.current !== null) window.clearTimeout(locationMessageTimerRef.current);
    setLocationStatus("error");
    setLocationError(message);
    locationMessageTimerRef.current = window.setTimeout(() => {
      locationMessageTimerRef.current = null;
      setLocationStatus("idle");
      setLocationError("");
    }, 4_500);
  }, []);

  const requestBeachLocation = useCallback(() => {
    if (locationStatus === "requesting") return;
    if (locationMessageTimerRef.current !== null) {
      window.clearTimeout(locationMessageTimerRef.current);
      locationMessageTimerRef.current = null;
    }
    if (!navigator.geolocation) {
      setBeachEnvironment(DEFAULT_BEACH_ENVIRONMENT);
      showLocationError("当前浏览器不支持定位，已使用北京时间。");
      return;
    }
    setLocationStatus("requesting");
    setLocationError("");
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        const latitude = Number(coords.latitude);
        const longitude = Number(coords.longitude);
        if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
          resetBeachLocation();
          showLocationError("定位数据无效，已使用北京时间。");
          return;
        }
        let timeZone = DEFAULT_TIME_ZONE;
        try {
          timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || DEFAULT_TIME_ZONE;
        } catch {
          // Keep the deterministic default when the browser cannot expose its IANA zone.
        }
        setBeachEnvironment({ locationEnabled: true, timeZone, coordinates: { latitude, longitude } });
        setLocationStatus("enabled");
        setLocationError("");
      },
      (cause) => {
        resetBeachLocation();
        showLocationError(cause.code === 1 ? "定位权限被拒绝，已使用北京时间。" : "暂时无法定位，已使用北京时间。");
      },
      { maximumAge: 300_000, timeout: 10_000 },
    );
  }, [locationStatus, resetBeachLocation, showLocationError]);

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
  useEffect(() => () => {
    if (locationMessageTimerRef.current !== null) window.clearTimeout(locationMessageTimerRef.current);
  }, []);

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
      <OceanScene
        transition={oceanTransition}
        effectsEnabled={authEffectsEnabled}
        environment={beachEnvironment}
        onFallbackChange={setOceanFallback}
      />
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
      <div className="public-controls">
        <div className="location-control">
          <button
            type="button"
            className={`location-button${locationStatus === "enabled" ? " location-button--enabled" : ""}`}
            aria-pressed={locationStatus === "enabled"}
            disabled={locationStatus === "requesting"}
            onClick={locationStatus === "enabled" ? resetBeachLocation : requestBeachLocation}
          >
            {locationStatus === "requesting" ? "正在获取位置…" : locationStatus === "enabled" ? "已启用位置光照" : "位置光照"}
          </button>
          {locationError ? <span className="location-control-status" role="status" aria-live="polite">{locationError}</span> : null}
        </div>
        <Tooltip title={oceanFallback ? "当前设备或浏览器已启用静态海滩，认证特效不可用。" : "控制登录与注册卡片的水花过渡。"}>
          <div className="auth-effects-toggle">
            <Switch size="small" checked={authEffectsEnabled} disabled={oceanFallback} onChange={setAuthEffectsEnabled} aria-label="认证特效" />
            <span>认证特效</span>
          </div>
        </Tooltip>
      </div>
    </div>
  );
}
