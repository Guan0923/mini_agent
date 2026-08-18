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
  const particleGeometry = { setAttribute: vi.fn(), dispose: vi.fn() };
  const coastlineGeometry = { setAttribute: vi.fn(), dispose: vi.fn() };
  const skyGeometry = { dispose: vi.fn() };
  const state: {
    shouldThrow: boolean;
    materialOptions: Array<Record<string, any>>;
    materials: Array<{ dispose: ReturnType<typeof vi.fn> }>;
    bufferGeometryCalls: number;
  } = { shouldThrow: false, materialOptions: [], materials: [], bufferGeometryCalls: 0 };

  return {
    state,
    renderer,
    surfaceGeometry,
    particleGeometry,
    coastlineGeometry,
    skyGeometry,
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
    BufferGeometry: vi.fn(() => {
      state.bufferGeometryCalls += 1;
      return state.bufferGeometryCalls === 1 ? particleGeometry : coastlineGeometry;
    }),
    SphereGeometry: vi.fn(() => skyGeometry),
    BufferAttribute: vi.fn((array: Float32Array, itemSize: number) => ({ array, itemSize })),
    ShaderMaterial: vi.fn((options: Record<string, any>) => {
      state.materialOptions.push(options);
      const material = { dispose: vi.fn() };
      state.materials.push(material);
      return material;
    }),
    Mesh: vi.fn(() => ({ rotation: { x: 0 }, position: { set: vi.fn() }, renderOrder: 0 })),
    Points: vi.fn(() => ({ position: { set: vi.fn() } })),
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
  BufferGeometry: threeState.BufferGeometry,
  SphereGeometry: threeState.SphereGeometry,
  BufferAttribute: threeState.BufferAttribute,
  ShaderMaterial: threeState.ShaderMaterial,
  Mesh: threeState.Mesh,
  Points: threeState.Points,
  Vector3: threeState.Vector3,
  DoubleSide: 2,
  BackSide: 4,
  AdditiveBlending: 3,
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
    threeState.BufferGeometry.mockClear();
    threeState.SphereGeometry.mockClear();
    threeState.BufferAttribute.mockClear();
    threeState.ShaderMaterial.mockClear();
    threeState.Mesh.mockClear();
    threeState.Points.mockClear();
    threeState.renderer.render.mockClear();
    threeState.renderer.dispose.mockClear();
    threeState.surfaceGeometry.dispose.mockClear();
    threeState.particleGeometry.setAttribute.mockClear();
    threeState.particleGeometry.dispose.mockClear();
    threeState.coastlineGeometry.setAttribute.mockClear();
    threeState.coastlineGeometry.dispose.mockClear();
    threeState.skyGeometry.dispose.mockClear();
    threeState.state.bufferGeometryCalls = 0;
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

  it("couples the ambient ocean, eruption, foam, and Three.js droplet layer", () => {
    const { unmount } = render(<OceanScene />);
    const surface = threeState.state.materialOptions[0];
    const particles = threeState.state.materialOptions[1];

    expect(surface.vertexShader).toContain("ambientScale");
    expect(surface.vertexShader).toContain("uTransitionDirection");
    expect(surface.fragmentShader).toContain("vTransitionFoam");
    expect(particles.vertexShader).toContain("uParticleProgress");
    expect(threeState.particleGeometry.setAttribute).toHaveBeenCalledTimes(5);
    expect(threeState.Points).toHaveBeenCalledOnce();

    act(() => {
      frameCallback?.(performance.now() + 100);
    });
    expect(surface.uniforms.uTime.value).toBeGreaterThan(0);
    expect(threeState.renderer.render).toHaveBeenCalled();

    unmount();
    expect(threeState.surfaceGeometry.dispose).toHaveBeenCalledOnce();
    expect(threeState.particleGeometry.dispose).toHaveBeenCalledOnce();
    expect(threeState.state.materials[0].dispose).toHaveBeenCalledOnce();
    expect(threeState.state.materials[1].dispose).toHaveBeenCalledOnce();
    expect(threeState.renderer.dispose).toHaveBeenCalledOnce();
  });

  it("adds the beach layers and updates sun uniforms from the environment", () => {
    const { container, unmount } = render(
      <OceanScene environment={{ locationEnabled: true, timeZone: "Asia/Shanghai", coordinates: { latitude: 31.23, longitude: 121.47 } }} />,
    );
    const surface = threeState.state.materialOptions[0];
    const sky = threeState.state.materialOptions[2];
    const coastline = threeState.state.materialOptions[3];

    expect(threeState.SphereGeometry).toHaveBeenCalledOnce();
    expect(surface.vertexShader).toContain("shorelineY");
    expect(surface.fragmentShader).toContain("uWetSandStrength");
    expect(sky.fragmentShader).toContain("cloudNoise");
    expect(coastline.fragmentShader).toContain("uSkyPhase");

    act(() => frameCallback?.(performance.now() + 100));
    expect(surface.uniforms.uSunIntensity.value).toBeGreaterThanOrEqual(0);
    expect(surface.uniforms.uSunDirection.value.y).toBeGreaterThan(-1);
    expect(container.querySelector(".ocean-scene")).toBeInTheDocument();

    unmount();
    expect(threeState.skyGeometry.dispose).toHaveBeenCalledOnce();
    expect(threeState.coastlineGeometry.dispose).toHaveBeenCalledOnce();
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
    expect(canvas).toBeTruthy();

    act(() => canvas?.dispatchEvent(new Event("webglcontextlost", { cancelable: true })));
    expect(container.querySelector(".ocean-scene-fallback")).toBeInTheDocument();
    act(() => canvas?.dispatchEvent(new Event("webglcontextrestored")));
    expect(threeState.WebGLRenderer).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".ocean-scene-fallback")).not.toBeInTheDocument();
  });

  it("starts and decays phase-aware eruption uniforms", () => {
    const { rerender } = render(<OceanScene />);
    const surface = threeState.state.materialOptions[0].uniforms;
    const particles = threeState.state.materialOptions[1].uniforms;

    rerender(<OceanScene transition={{ token: "auth-1", phase: "emerge" }} />);
    expect(surface.uTransitionStrength.value).toBe(1);
    expect(surface.uTransitionDirection.value).toBe(1);
    expect(particles.uParticleStrength.value).toBe(1);

    act(() => {
      frameCallback?.(performance.now() + 525);
    });
    expect(surface.uTransitionProgress.value).toBeGreaterThan(0);
    expect(surface.uTransitionProgress.value).toBeLessThan(1);

    act(() => {
      frameCallback?.(performance.now() + 1200);
    });
    expect(surface.uTransitionStrength.value).toBe(0);
    expect(particles.uParticleStrength.value).toBe(0);
  });

  it("uses a depression for sink and resets all event uniforms when disabled", () => {
    const { rerender } = render(<OceanScene transition={{ token: "sink-1", phase: "sink" }} />);
    const surface = threeState.state.materialOptions[0].uniforms;
    const particles = threeState.state.materialOptions[1].uniforms;
    expect(surface.uTransitionDirection.value).toBe(-1);
    expect(surface.uTransitionStrength.value).toBeGreaterThan(0);

    rerender(<OceanScene transition={{ token: "sink-1", phase: "sink" }} effectsEnabled={false} />);
    expect(surface.uTransitionProgress.value).toBe(0);
    expect(surface.uTransitionStrength.value).toBe(0);
    expect(surface.uTransitionDirection.value).toBe(0);
    expect(particles.uParticleStrength.value).toBe(0);
  });

  it("reports fallback capability to the public layout", () => {
    reducedMotion = true;
    const onFallbackChange = vi.fn();
    render(<OceanScene onFallbackChange={onFallbackChange} />);
    expect(onFallbackChange).toHaveBeenCalledWith(true);
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
