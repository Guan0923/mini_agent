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
  const geometry = { dispose: vi.fn() };
  const material = { dispose: vi.fn() };
  const state: { shouldThrow: boolean; materialOptions: Record<string, any> | null } = {
    shouldThrow: false,
    materialOptions: null,
  };

  return {
    state,
    renderer,
    geometry,
    material,
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
    PlaneGeometry: vi.fn(() => geometry),
    ShaderMaterial: vi.fn((options: Record<string, any>) => {
      state.materialOptions = options;
      return material;
    }),
    Mesh: vi.fn(() => ({ rotation: { x: 0 } })),
  };
});

vi.mock("three", () => ({
  WebGLRenderer: threeState.WebGLRenderer,
  Scene: threeState.Scene,
  PerspectiveCamera: threeState.PerspectiveCamera,
  PlaneGeometry: threeState.PlaneGeometry,
  ShaderMaterial: threeState.ShaderMaterial,
  Mesh: threeState.Mesh,
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
    threeState.state.materialOptions = null;
    threeState.WebGLRenderer.mockClear();
    threeState.Scene.mockClear();
    threeState.PerspectiveCamera.mockClear();
    threeState.PlaneGeometry.mockClear();
    threeState.ShaderMaterial.mockClear();
    threeState.Mesh.mockClear();
    threeState.renderer.render.mockClear();
    threeState.renderer.dispose.mockClear();
    threeState.geometry.dispose.mockClear();
    threeState.material.dispose.mockClear();
    Object.defineProperty(navigator, "hardwareConcurrency", { configurable: true, value: 4 });
    Object.defineProperty(navigator, "deviceMemory", { configurable: true, value: 4 });
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

  it("creates the intensified wave shader and advances its time uniform", () => {
    const { unmount } = render(<OceanScene />);
    const shader = String(threeState.state.materialOptions?.vertexShader);

    expect(shader).toContain("uTime * 1.04");
    expect(shader).toContain("vec2(-0.78, -0.94)");
    expect(frameCallback).toBeTypeOf("function");

    act(() => {
      frameCallback?.(performance.now() + 100);
    });

    expect(threeState.state.materialOptions?.uniforms.uTime.value).toBeGreaterThan(0);
    expect(threeState.renderer.render).toHaveBeenCalled();
    unmount();
    expect(threeState.geometry.dispose).toHaveBeenCalledOnce();
    expect(threeState.material.dispose).toHaveBeenCalledOnce();
    expect(threeState.renderer.dispose).toHaveBeenCalledOnce();
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
