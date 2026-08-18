import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const threeState = vi.hoisted(() => {
  const renderer = {
    outputColorSpace: "",
    toneMapping: 0,
    toneMappingExposure: 0,
    setClearColor: vi.fn(),
    setPixelRatio: vi.fn(),
    setSize: vi.fn(),
    render: vi.fn(),
    dispose: vi.fn(),
  };
  const surfaceGeometry = { dispose: vi.fn() };
  const state: {
    shouldThrow: boolean;
    materialOptions: Array<Record<string, any>>;
    materials: Array<{ dispose: ReturnType<typeof vi.fn> }>;
  } = { shouldThrow: false, materialOptions: [], materials: [] };

  return {
    state,
    renderer,
    surfaceGeometry,
    WebGLRenderer: vi.fn(() => {
      if (state.shouldThrow) throw new Error("WebGL unavailable");
      return renderer;
    }),
    Scene: vi.fn(() => ({ add: vi.fn() })),
    PerspectiveCamera: vi.fn(() => ({
      position: { set: vi.fn() },
      lookAt: vi.fn(),
      aspect: 1,
      updateProjectionMatrix: vi.fn(),
    })),
    PlaneGeometry: vi.fn(() => surfaceGeometry),
    ShaderMaterial: vi.fn((options: Record<string, any>) => {
      state.materialOptions.push(options);
      const material = { dispose: vi.fn() };
      state.materials.push(material);
      return material;
    }),
    Mesh: vi.fn(() => ({ rotation: { x: 0 } })),
    Vector3: class Vector3 {
      constructor(public x = 0, public y = 0, public z = 0) {}
      set(x: number, y: number, z: number) { this.x = x; this.y = y; this.z = z; return this; }
      clone() { return new Vector3(this.x, this.y, this.z); }
      lerp(target: Vector3, alpha: number) {
        this.x += (target.x - this.x) * alpha;
        this.y += (target.y - this.y) * alpha;
        this.z += (target.z - this.z) * alpha;
        return this;
      }
    },
  };
});

vi.mock("three", () => ({
  WebGLRenderer: threeState.WebGLRenderer,
  Scene: threeState.Scene,
  PerspectiveCamera: threeState.PerspectiveCamera,
  PlaneGeometry: threeState.PlaneGeometry,
  ShaderMaterial: threeState.ShaderMaterial,
  Mesh: threeState.Mesh,
  Vector3: threeState.Vector3,
  DoubleSide: 2,
  SRGBColorSpace: "srgb",
  ACESFilmicToneMapping: 1,
}));

import OceanScene from "./OceanScene";

describe("OceanScene", () => {
  let frameCallback: FrameRequestCallback | null;
  let reducedMotion = false;

  beforeEach(() => {
    frameCallback = null;
    reducedMotion = false;
    threeState.state.shouldThrow = false;
    threeState.state.materialOptions = [];
    threeState.state.materials = [];
    threeState.WebGLRenderer.mockClear();
    threeState.Scene.mockClear();
    threeState.PerspectiveCamera.mockClear();
    threeState.PlaneGeometry.mockClear();
    threeState.ShaderMaterial.mockClear();
    threeState.Mesh.mockClear();
    threeState.renderer.render.mockClear();
    threeState.renderer.dispose.mockClear();
    threeState.surfaceGeometry.dispose.mockClear();
    Object.defineProperty(navigator, "hardwareConcurrency", { configurable: true, value: 4 });
    Object.defineProperty(navigator, "deviceMemory", { configurable: true, value: 4 });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 0 });
    window.matchMedia = ((query: string) => ({
      matches: reducedMotion,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      frameCallback = callback;
      return 1;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
  });

  it("renders only the ambient sea, sand, and shoreline foam material", () => {
    const { unmount } = render(<OceanScene />);
    const surface = threeState.state.materialOptions[0];

    expect(threeState.state.materialOptions).toHaveLength(1);
    expect(surface.vertexShader).toContain("waterHeight");
    expect(surface.vertexShader).toContain("shorelineY");
    expect(surface.vertexShader).not.toContain("uTransition");
    expect(surface.fragmentShader).toContain("shoreFoam");
    expect(surface.fragmentShader).toContain("drySand");
    expect(surface.fragmentShader).not.toContain("eventFoam");

    act(() => frameCallback?.(performance.now() + 100));
    expect(surface.uniforms.uTime.value).toBeGreaterThan(0);
    expect(threeState.renderer.render).toHaveBeenCalled();

    unmount();
    expect(threeState.surfaceGeometry.dispose).toHaveBeenCalledOnce();
    expect(threeState.state.materials[0].dispose).toHaveBeenCalledOnce();
    expect(threeState.renderer.dispose).toHaveBeenCalledOnce();
  });

  it("seeds the first frame with the current time's sun instead of a fixed daytime light", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-06-21T16:00:00.000Z"));
    try {
      render(<OceanScene />);
      const surface = threeState.state.materialOptions[0];

      expect(surface.uniforms.uSunIntensity.value).toBe(0);
      expect(surface.uniforms.uSunColor.value.x).toBeCloseTo(0.38, 5);
      expect(surface.uniforms.uSunColor.value.y).toBeCloseTo(0.52, 5);
      expect(surface.uniforms.uSunColor.value.z).toBeCloseTo(0.82, 5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("updates sea and sand lighting from the beach environment", () => {
    const { container } = render(
      <OceanScene environment={{ locationEnabled: true, timeZone: "Asia/Shanghai", coordinates: { latitude: 31.23, longitude: 121.47 } }} />,
    );
    const surface = threeState.state.materialOptions[0];

    expect(surface.uniforms.uSunColor).toBeDefined();
    expect(surface.uniforms.uSunIntensity).toBeDefined();
    expect(surface.uniforms.uWetSandStrength).toBeDefined();
    act(() => frameCallback?.(performance.now() + 100));
    expect(surface.uniforms.uSunIntensity.value).toBeGreaterThanOrEqual(0);
    expect(surface.uniforms.uSunDirection.value.y).toBeGreaterThan(-1);
    expect(container.querySelector(".ocean-scene")).toBeInTheDocument();
  });

  it("uses the mobile quality profile on touch devices", () => {
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    render(<OceanScene />);
    expect(threeState.PlaneGeometry).toHaveBeenCalledWith(320, 320, 112, 84);
    expect(threeState.renderer.setPixelRatio).toHaveBeenCalledWith(1);
  });

  it("falls back on context loss and resumes after context restoration", () => {
    const { container } = render(<OceanScene />);
    const canvas = container.querySelector("canvas");

    act(() => canvas?.dispatchEvent(new Event("webglcontextlost", { cancelable: true })));
    expect(container.querySelector(".ocean-scene-fallback")).toBeInTheDocument();
    act(() => canvas?.dispatchEvent(new Event("webglcontextrestored")));
    expect(threeState.WebGLRenderer).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".ocean-scene-fallback")).not.toBeInTheDocument();
  });

  it("uses the static fallback for reduced motion", () => {
    reducedMotion = true;
    const { container } = render(<OceanScene />);
    expect(container.querySelector(".ocean-scene-fallback")).toBeInTheDocument();
    expect(threeState.WebGLRenderer).not.toHaveBeenCalled();
  });

  it("uses the static fallback on low-power hardware", () => {
    Object.defineProperty(navigator, "hardwareConcurrency", { configurable: true, value: 2 });
    const { container } = render(<OceanScene />);
    expect(container.querySelector(".ocean-scene-fallback")).toBeInTheDocument();
  });

  it("uses the static fallback when WebGL creation fails", () => {
    threeState.state.shouldThrow = true;
    const { container } = render(<OceanScene />);
    expect(container.querySelector(".ocean-scene-fallback")).toBeInTheDocument();
  });
});
