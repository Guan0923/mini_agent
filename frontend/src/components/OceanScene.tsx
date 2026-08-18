import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { calculateSunPosition, DEFAULT_TIME_ZONE, type GeoPoint } from "./beach/sunPosition";

const surfaceVertexShader = `
uniform float uTime;
uniform float uShorelineOffset;
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;
varying float vSandMask;
varying float vShoreDistance;

float shorelineY(float x) {
  return -6.0 + uShorelineOffset
    + sin(x * 0.055) * 2.5
    + sin(x * 0.13 + 1.7) * 0.85;
}

float sandMaskAt(vec2 point) {
  float shore = shorelineY(point.x);
  return 1.0 - smoothstep(shore - 2.4, shore + 2.4, point.y);
}

float waterHeight(vec2 point) {
  float height = sin(dot(point, vec2(0.56, 0.26)) + uTime * 0.82) * 0.30;
  height += sin(dot(point, vec2(-0.24, 0.52)) - uTime * 0.62) * 0.21;
  height += cos(dot(point, vec2(0.16, 0.14)) + uTime * 0.38) * 0.27;
  height += sin(length(point + vec2(9.0, -6.0)) * 0.30 - uTime * 0.48) * 0.11;
  height += sin(dot(point, vec2(0.92, -0.68)) + uTime * 1.04) * 0.065;
  height += cos(dot(point, vec2(-0.78, -0.94)) - uTime * 0.88) * 0.045;
  return height;
}

float sandHeight(vec2 point) {
  return 0.03
    + sin(point.x * 0.22 + point.y * 0.11) * 0.035
    + cos(point.x * 0.08 - point.y * 0.19) * 0.025;
}

float surfaceHeight(vec2 point) {
  float sand = sandMaskAt(point);
  return mix(waterHeight(point), sandHeight(point), sand);
}

void main() {
  vec3 p = position;
  float stepSize = 0.16;
  float shore = shorelineY(p.x);
  float sand = sandMaskAt(p.xy);
  float height = surfaceHeight(p.xy);
  float heightX = surfaceHeight(p.xy + vec2(stepSize, 0.0));
  float heightY = surfaceHeight(p.xy + vec2(0.0, stepSize));
  p.z = height;

  vec3 objectNormal = normalize(vec3(height - heightX, height - heightY, stepSize));
  vec4 worldPosition = modelMatrix * vec4(p, 1.0);
  vWorldPosition = worldPosition.xyz;
  vWorldNormal = normalize(mat3(modelMatrix) * objectNormal);
  vElevation = height;
  vSandMask = sand;
  vShoreDistance = abs(p.y - shore);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}
`;

const surfaceFragmentShader = `
uniform vec3 uSunDirection;
uniform vec3 uSunColor;
uniform float uSunIntensity;
uniform float uWetSandStrength;
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;
varying float vSandMask;
varying float vShoreDistance;

void main() {
  vec3 normal = normalize(vWorldNormal);
  vec3 viewDirection = normalize(cameraPosition - vWorldPosition);
  vec3 lightDirection = normalize(uSunDirection);
  vec3 halfDirection = normalize(lightDirection + viewDirection);
  float light = max(dot(normal, lightDirection), 0.0) * (0.32 + uSunIntensity * 0.68);
  float fresnel = pow(1.0 - max(dot(normal, viewDirection), 0.0), 3.2);
  float highlight = pow(max(dot(normal, halfDirection), 0.0), 64.0) * (0.2 + uSunIntensity * 0.8);
  float crest = smoothstep(0.30, 0.58, vElevation);
  float foamPattern = sin(vWorldPosition.x * 0.72 + vWorldPosition.z * 0.31) * 0.5 + 0.5;
  float foam = crest * smoothstep(0.42, 0.78, foamPattern) * 0.38;
  float shoreFoam = (1.0 - smoothstep(0.1, 2.8, vShoreDistance)) * (1.0 - vSandMask * 0.18);

  vec3 lagoon = vec3(0.018, 0.27, 0.40);
  vec3 turquoise = vec3(0.02, 0.64, 0.70);
  vec3 sunlitSea = mix(vec3(0.10, 0.48, 0.58), uSunColor, 0.18);
  vec3 sea = mix(lagoon, turquoise, clamp(light * 0.78 + 0.12, 0.0, 1.0));
  sea = mix(sea, sunlitSea, fresnel * 0.56);
  sea += uSunColor * highlight * 0.72;

  float sandPattern = sin(vWorldPosition.x * 0.32 + vWorldPosition.z * 0.17) * 0.5 + 0.5;
  vec3 drySand = mix(vec3(0.72, 0.38, 0.12), vec3(0.98, 0.68, 0.27), sandPattern);
  vec3 wetSand = mix(vec3(0.12, 0.34, 0.34), vec3(0.50, 0.55, 0.40), sandPattern);
  float wetness = vSandMask * (1.0 - smoothstep(0.5, 7.5, vShoreDistance)) * uWetSandStrength;
  vec3 sand = mix(drySand, wetSand, wetness);
  sand *= 0.48 + uSunIntensity * 0.60;

  vec3 color = mix(sea, sand, vSandMask);
  float allFoam = max(foam, shoreFoam * 0.92);
  color = mix(color, vec3(0.93, 1.0, 0.96), allFoam);
  float haze = smoothstep(70.0, 170.0, length(cameraPosition - vWorldPosition));
  color = mix(color, mix(vec3(0.10, 0.38, 0.48), vec3(0.76, 0.68, 0.42), vSandMask), haze * 0.24);
  gl_FragColor = vec4(color, 1.0);
}
`;

export interface BeachEnvironment {
  locationEnabled: boolean;
  timeZone: string;
  coordinates: GeoPoint | null;
}

interface OceanSceneProps {
  environment?: BeachEnvironment;
}

interface SharedUniforms {
  [uniform: string]: { value: unknown };
  uTime: { value: number };
  uSunDirection: { value: THREE.Vector3 };
  uSunColor: { value: THREE.Vector3 };
  uSunIntensity: { value: number };
  uShorelineOffset: { value: number };
  uWetSandStrength: { value: number };
}

interface RenderQuality {
  surfaceX: number;
  surfaceY: number;
  dpr: number;
}

const DEFAULT_ENVIRONMENT: BeachEnvironment = {
  locationEnabled: false,
  timeZone: DEFAULT_TIME_ZONE,
  coordinates: null,
};

function qualityForDevice(): RenderQuality {
  const touchDevice = navigator.maxTouchPoints > 0 || window.innerWidth <= 768;
  if (touchDevice) {
    return {
      surfaceX: 112,
      surfaceY: 84,
      dpr: 1.25,
    };
  }
  return {
    surfaceX: 180,
    surfaceY: 140,
    dpr: Math.min(window.devicePixelRatio || 1, 1.6),
  };
}

function isLowPowerDevice(): boolean {
  const hints = navigator as Navigator & { deviceMemory?: number };
  return (navigator.hardwareConcurrency ?? 4) <= 2 || (hints.deviceMemory ?? 4) <= 2;
}

function normalizeEnvironment(environment?: BeachEnvironment): BeachEnvironment {
  if (!environment) return DEFAULT_ENVIRONMENT;
  return {
    locationEnabled: Boolean(environment.locationEnabled && environment.coordinates),
    timeZone: environment.timeZone || DEFAULT_TIME_ZONE,
    coordinates: environment.coordinates,
  };
}

export default function OceanScene({ environment }: OceanSceneProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(() => (
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ));
  const environmentRef = useRef<BeachEnvironment>(normalizeEnvironment(environment));

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return;
    const handleChange = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    media.addEventListener?.("change", handleChange);
    return () => media.removeEventListener?.("change", handleChange);
  }, []);

  useEffect(() => {
    environmentRef.current = normalizeEnvironment(environment);
  }, [environment]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (reducedMotion || isLowPowerDevice()) {
      setFallback(true);
      return;
    }

    let renderer: THREE.WebGLRenderer;
    const configureRenderer = (instance: THREE.WebGLRenderer) => {
      instance.outputColorSpace = THREE.SRGBColorSpace;
      instance.toneMapping = THREE.ACESFilmicToneMapping;
      instance.toneMappingExposure = 1.08;
      instance.setClearColor(0xbcefeb, 0);
    };
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
    } catch {
      setFallback(true);
      return;
    }

    const quality = qualityForDevice();
    setFallback(false);
    configureRenderer(renderer);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 400);
    camera.position.set(0, 9.2, 15.5);
    camera.lookAt(0, 0, -8);
    const initialEnvironment = environmentRef.current;
    const initialSun = calculateSunPosition(
      new Date(),
      initialEnvironment.timeZone,
      initialEnvironment.locationEnabled ? initialEnvironment.coordinates : null,
    );
    const sharedUniforms: SharedUniforms = {
      uTime: { value: 0 },
      uSunDirection: { value: new THREE.Vector3(...initialSun.direction) },
      uSunColor: { value: new THREE.Vector3(...initialSun.color) },
      uSunIntensity: { value: initialSun.intensity },
      uShorelineOffset: { value: 0 },
      uWetSandStrength: { value: 0.88 },
    };

    const surfaceGeometry = new THREE.PlaneGeometry(320, 320, quality.surfaceX, quality.surfaceY);
    const surfaceMaterial = new THREE.ShaderMaterial({
      uniforms: sharedUniforms,
      vertexShader: surfaceVertexShader,
      fragmentShader: surfaceFragmentShader,
      side: THREE.DoubleSide,
    });
    const ocean = new THREE.Mesh(surfaceGeometry, surfaceMaterial);
    ocean.rotation.x = -Math.PI / 2;

    scene.add(ocean);

    let frame: number | null = null;
    let lastTime = performance.now();
    let visible = !document.hidden;
    let contextLostState = false;
    let permanentlyFallback = false;
    let lastSunMinute = -1;
    let lastEnvironmentKey = "";
    const targetSunDirection = sharedUniforms.uSunDirection.value.clone();
    const targetSunColor = sharedUniforms.uSunColor.value.clone();
    let targetSunIntensity = sharedUniforms.uSunIntensity.value;
    const updateSunTarget = () => {
      const current = new Date();
      const environmentValue = environmentRef.current;
      const sun = calculateSunPosition(current, environmentValue.timeZone, environmentValue.locationEnabled ? environmentValue.coordinates : null);
      targetSunDirection.set(...sun.direction);
      targetSunColor.set(...sun.color);
      targetSunIntensity = sun.intensity;
      lastSunMinute = Math.floor(current.getTime() / 60_000);
      lastEnvironmentKey = `${environmentValue.locationEnabled}:${environmentValue.timeZone}:${environmentValue.coordinates?.latitude ?? ""}:${environmentValue.coordinates?.longitude ?? ""}`;
    };
    const resize = () => {
      const width = Math.max(1, canvas.clientWidth || window.innerWidth);
      const height = Math.max(1, canvas.clientHeight || window.innerHeight);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, quality.dpr));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    const schedule = () => {
      if (frame === null && visible && !contextLostState) frame = requestAnimationFrame(render);
    };
    const render = (now: number) => {
      frame = null;
      if (!visible || contextLostState) return;
      const delta = Math.min(0.05, (now - lastTime) / 1000);
      lastTime = now;
      const currentMinute = Math.floor(Date.now() / 60_000);
      const currentEnvironment = environmentRef.current;
      const environmentKey = `${currentEnvironment.locationEnabled}:${currentEnvironment.timeZone}:${currentEnvironment.coordinates?.latitude ?? ""}:${currentEnvironment.coordinates?.longitude ?? ""}`;
      if (currentMinute !== lastSunMinute || environmentKey !== lastEnvironmentKey) updateSunTarget();
      sharedUniforms.uTime.value += delta;
      sharedUniforms.uShorelineOffset.value = Math.sin(sharedUniforms.uTime.value * 0.08) * 0.65;
      sharedUniforms.uWetSandStrength.value = 0.84 + Math.sin(sharedUniforms.uTime.value * 0.08) * 0.08;
      sharedUniforms.uSunDirection.value.lerp(targetSunDirection, Math.min(1, delta * 1.8));
      sharedUniforms.uSunColor.value.lerp(targetSunColor, Math.min(1, delta * 1.8));
      sharedUniforms.uSunIntensity.value += (targetSunIntensity - sharedUniforms.uSunIntensity.value) * Math.min(1, delta * 1.8);
      renderer.render(scene, camera);
      schedule();
    };
    const visibility = () => {
      visible = !document.hidden;
      if (visible) {
        lastTime = performance.now();
        schedule();
      } else if (frame !== null) {
        cancelAnimationFrame(frame);
        frame = null;
      }
    };
    const contextLost = (event: Event) => {
      event.preventDefault();
      contextLostState = true;
      visible = false;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      setFallback(true);
    };
    const contextRestored = () => {
      if (permanentlyFallback) return;
      try {
        const previousRenderer = renderer;
        const replacementRenderer = new THREE.WebGLRenderer({
          canvas,
          alpha: true,
          antialias: true,
          powerPreference: "high-performance",
        });
        configureRenderer(replacementRenderer);
        renderer = replacementRenderer;
        previousRenderer.dispose();
      } catch {
        permanentlyFallback = true;
        contextLostState = true;
        visible = false;
        setFallback(true);
        return;
      }
      contextLostState = false;
      visible = !document.hidden;
      lastTime = performance.now();
      updateSunTarget();
      resize();
      setFallback(false);
      schedule();
    };

    updateSunTarget();
    resize();
    window.addEventListener("resize", resize);
    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
    resizeObserver?.observe(canvas.parentElement ?? canvas);
    document.addEventListener("visibilitychange", visibility);
    canvas.addEventListener("webglcontextlost", contextLost, { passive: false });
    canvas.addEventListener("webglcontextrestored", contextRestored);
    schedule();

    return () => {
      contextLostState = true;
      if (frame !== null) cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      resizeObserver?.disconnect();
      document.removeEventListener("visibilitychange", visibility);
      canvas.removeEventListener("webglcontextlost", contextLost);
      canvas.removeEventListener("webglcontextrestored", contextRestored);
      surfaceGeometry.dispose();
      surfaceMaterial.dispose();
      renderer.dispose();
    };
  }, [reducedMotion]);

  return (
    <div className={`ocean-scene${fallback ? " ocean-scene-fallback" : ""}`} aria-hidden="true">
      <canvas ref={canvasRef} className="ocean-canvas" />
      <div className="ocean-haze" />
    </div>
  );
}
