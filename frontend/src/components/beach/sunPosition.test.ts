import { describe, expect, it } from "vitest";
import { calculateSunPosition, DEFAULT_TIME_ZONE } from "./sunPosition";

describe("calculateSunPosition", () => {
  it("uses Beijing time for the default stylized day", () => {
    const dawn = calculateSunPosition(new Date("2024-06-21T22:00:00.000Z"), DEFAULT_TIME_ZONE);
    const noon = calculateSunPosition(new Date("2024-06-21T04:00:00.000Z"), DEFAULT_TIME_ZONE);
    const dusk = calculateSunPosition(new Date("2024-06-21T09:30:00.000Z"), DEFAULT_TIME_ZONE);
    const night = calculateSunPosition(new Date("2024-06-21T16:00:00.000Z"), DEFAULT_TIME_ZONE);

    expect(dawn.phase).toBe("dawn");
    expect(noon.phase).toBe("day");
    expect(dusk.phase).toBe("dusk");
    expect(night.phase).toBe("night");
    expect(noon.intensity).toBeGreaterThan(dawn.intensity);
    expect(noon.intensity).toBeGreaterThan(dusk.intensity);
    expect(night.intensity).toBe(0);
  });

  it("respects a daylight-saving IANA timezone", () => {
    const noon = calculateSunPosition(new Date("2024-06-21T16:00:00.000Z"), "America/New_York");
    expect(noon.phase).toBe("day");
    expect(noon.intensity).toBeGreaterThan(0.5);
  });

  it("uses the coordinate-aware solar approximation when location is enabled", () => {
    const sun = calculateSunPosition(
      new Date("2024-06-21T04:00:00.000Z"),
      DEFAULT_TIME_ZONE,
      { latitude: 31.23, longitude: 121.47 },
    );
    expect(sun.phase).toBe("day");
    expect(sun.intensity).toBeGreaterThan(0.5);
    expect(sun.direction[1]).toBeGreaterThan(0);
  });

  it("falls back to the default time curve for invalid coordinates", () => {
    const invalid = calculateSunPosition(
      new Date("2024-06-21T04:00:00.000Z"),
      DEFAULT_TIME_ZONE,
      { latitude: 120, longitude: 300 },
    );
    const defaultSun = calculateSunPosition(new Date("2024-06-21T04:00:00.000Z"), DEFAULT_TIME_ZONE);
    expect(invalid.phase).toBe(defaultSun.phase);
    expect(invalid.intensity).toBe(defaultSun.intensity);
  });
});
