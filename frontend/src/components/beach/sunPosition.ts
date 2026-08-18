export const DEFAULT_TIME_ZONE = "Asia/Shanghai";

export interface GeoPoint {
  latitude: number;
  longitude: number;
}

export type SunPhase = "night" | "dawn" | "day" | "dusk";

export interface SunPosition {
  direction: [number, number, number];
  intensity: number;
  phase: SunPhase;
  color: [number, number, number];
}

interface ZonedParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const DAYLIGHT_START = 6 * 60;
const DAYLIGHT_END = 18 * 60;
const DEG_TO_RAD = Math.PI / 180;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function smoothstep(edge0: number, edge1: number, value: number): number {
  const normalized = clamp((value - edge0) / (edge1 - edge0), 0, 1);
  return normalized * normalized * (3 - 2 * normalized);
}

function parseParts(date: Date, timeZone: string): ZonedParts {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const values = new Map(parts.map((part) => [part.type, Number(part.value)]));
    return {
      year: values.get("year") ?? 1970,
      month: values.get("month") ?? 1,
      day: values.get("day") ?? 1,
      hour: values.get("hour") ?? 0,
      minute: values.get("minute") ?? 0,
      second: values.get("second") ?? 0,
    };
  } catch {
    const fallback = new Intl.DateTimeFormat("en-US", {
      timeZone: DEFAULT_TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const values = new Map(fallback.map((part) => [part.type, Number(part.value)]));
    return {
      year: values.get("year") ?? 1970,
      month: values.get("month") ?? 1,
      day: values.get("day") ?? 1,
      hour: values.get("hour") ?? 0,
      minute: values.get("minute") ?? 0,
      second: values.get("second") ?? 0,
    };
  }
}

function localMinutes(parts: ZonedParts): number {
  return parts.hour * 60 + parts.minute + parts.second / 60;
}

function phaseForMinutes(minutes: number): SunPhase {
  if (minutes < DAYLIGHT_START || minutes >= DAYLIGHT_END) return "night";
  if (minutes < DAYLIGHT_START + 90) return "dawn";
  if (minutes >= DAYLIGHT_END - 90) return "dusk";
  return "day";
}

function sunColor(phase: SunPhase, intensity: number): [number, number, number] {
  if (phase === "night") return [0.38, 0.52, 0.82];
  if (phase === "day") return [1.0, 0.97, 0.82];
  const warmth = 1 - smoothstep(0.15, 0.72, intensity);
  return [1.0, 0.58 + warmth * 0.22, 0.34 + warmth * 0.18];
}

function normalizedDirection(azimuth: number, elevation: number): [number, number, number] {
  const horizontal = Math.cos(elevation);
  return [
    Math.sin(azimuth) * horizontal,
    Math.sin(elevation),
    Math.cos(azimuth) * horizontal,
  ];
}

function stylizedSun(minutes: number): SunPosition {
  const phase = phaseForMinutes(minutes);
  const daylightProgress = clamp((minutes - DAYLIGHT_START) / (DAYLIGHT_END - DAYLIGHT_START), 0, 1);
  const elevation = Math.sin(daylightProgress * Math.PI) * 1.12;
  const intensity = clamp(Math.sin(daylightProgress * Math.PI), 0, 1);
  const azimuth = -Math.PI * 0.72 + daylightProgress * Math.PI * 1.44;
  return {
    direction: normalizedDirection(azimuth, elevation),
    intensity,
    phase,
    color: sunColor(phase, intensity),
  };
}

function dayOfYear(parts: ZonedParts): number {
  const start = Date.UTC(parts.year, 0, 1);
  const current = Date.UTC(parts.year, parts.month - 1, parts.day);
  return Math.floor((current - start) / 86_400_000) + 1;
}

function timezoneOffsetMinutes(date: Date, timeZone: string): number {
  try {
    const value = new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "shortOffset",
      hour: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date).find((part) => part.type === "timeZoneName")?.value;
    if (!value || value === "GMT") return 0;
    const match = value.match(/GMT([+-])(\d{1,2})(?::(\d{2}))?/);
    if (!match) return 0;
    const minutes = Number(match[2]) * 60 + Number(match[3] ?? 0);
    return match[1] === "-" ? -minutes : minutes;
  } catch {
    return 0;
  }
}

function physicalSun(date: Date, timeZone: string, coordinates: GeoPoint, parts: ZonedParts): SunPosition {
  const latitude = clamp(coordinates.latitude, -90, 90) * DEG_TO_RAD;
  const longitude = clamp(coordinates.longitude, -180, 180);
  const ordinal = dayOfYear(parts);
  const offsetHours = timezoneOffsetMinutes(date, timeZone) / 60;
  const localHour = localMinutes(parts) / 60;
  const gamma = (2 * Math.PI / 365) * (ordinal - 1 + (localHour - 12) / 24);
  const declination = 0.006918
    - 0.399912 * Math.cos(gamma)
    + 0.070257 * Math.sin(gamma)
    - 0.006758 * Math.cos(2 * gamma)
    + 0.000907 * Math.sin(2 * gamma)
    - 0.002697 * Math.cos(3 * gamma)
    + 0.00148 * Math.sin(3 * gamma);
  const equationOfTime = 229.18 * (0.000075
    + 0.001868 * Math.cos(gamma)
    - 0.032077 * Math.sin(gamma)
    - 0.014615 * Math.cos(2 * gamma)
    - 0.040849 * Math.sin(2 * gamma));
  const solarMinutes = localMinutes(parts) + equationOfTime + 4 * longitude - 60 * offsetHours;
  const hourAngle = (solarMinutes / 4 - 180) * DEG_TO_RAD;
  const cosineZenith = clamp(
    Math.sin(latitude) * Math.sin(declination)
      + Math.cos(latitude) * Math.cos(declination) * Math.cos(hourAngle),
    -1,
    1,
  );
  const elevation = Math.asin(cosineZenith);
  const intensity = clamp(Math.sin(elevation), 0, 1);
  const azimuth = Math.atan2(
    Math.sin(hourAngle),
    Math.cos(hourAngle) * Math.sin(latitude) - Math.tan(declination) * Math.cos(latitude),
  ) + Math.PI;
  const minutes = localMinutes(parts);
  let phase = phaseForMinutes(minutes);
  if (intensity <= 0.02) phase = "night";
  else if (intensity < 0.28 && minutes < 12 * 60) phase = "dawn";
  else if (intensity < 0.28) phase = "dusk";
  return {
    direction: normalizedDirection(azimuth, Math.max(elevation, 0.01)),
    intensity,
    phase,
    color: sunColor(phase, intensity),
  };
}

export function calculateSunPosition(
  now: Date,
  timeZone: string = DEFAULT_TIME_ZONE,
  coordinates: GeoPoint | null = null,
): SunPosition {
  const parts = parseParts(now, timeZone || DEFAULT_TIME_ZONE);
  if (
    coordinates
    && Number.isFinite(coordinates.latitude)
    && Number.isFinite(coordinates.longitude)
    && coordinates.latitude >= -90
    && coordinates.latitude <= 90
    && coordinates.longitude >= -180
    && coordinates.longitude <= 180
  ) {
    return physicalSun(now, timeZone || DEFAULT_TIME_ZONE, coordinates, parts);
  }
  return stylizedSun(localMinutes(parts));
}
