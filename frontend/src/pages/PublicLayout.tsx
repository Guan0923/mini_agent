import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet } from "react-router-dom";
import OceanScene, { type BeachEnvironment } from "../components/OceanScene";
import { DEFAULT_TIME_ZONE } from "../components/beach/sunPosition";

const DEFAULT_BEACH_ENVIRONMENT: BeachEnvironment = {
  locationEnabled: false,
  timeZone: DEFAULT_TIME_ZONE,
  coordinates: null,
};

/** Keeps the public ocean scene mounted while foreground routes change. */
export default function PublicLayout() {
  const [beachEnvironment, setBeachEnvironment] = useState<BeachEnvironment>(DEFAULT_BEACH_ENVIRONMENT);
  const [locationStatus, setLocationStatus] = useState<"idle" | "requesting" | "enabled" | "error">("idle");
  const [locationError, setLocationError] = useState("");
  const locationMessageTimerRef = useRef<number | null>(null);

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

  useEffect(() => () => {
    if (locationMessageTimerRef.current !== null) window.clearTimeout(locationMessageTimerRef.current);
  }, []);

  return (
    <div className="public-shell">
      <OceanScene environment={beachEnvironment} />
      <Outlet />
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
      </div>
    </div>
  );
}
