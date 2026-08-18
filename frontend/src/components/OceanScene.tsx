import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { calculateSunPosition, DEFAULT_TIME_ZONE, type GeoPoint, type SunPhase } from "./beach/sunPosition";

const surfaceVertexShader = `
uniform float uTime;
uniform float uTransitionProgress;
uniform float uTransitionStrength;
uniform float uTransitionDirection;
uniform float uShorelineOffset;
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;
varying float vTransitionFoam;
varying float vSandMask;
varying float vShoreDistance;

float transitionEnvelope() {
  return sin(clamp(uTransitionProgress, 0.0, 1.0) * 3.14159265) * uTransitionStrength;
}

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
  float envelope = transitionEnvelope();
  float ambientScale = 1.0 + envelope * 0.82;
  float height = sin(dot(point, vec2(0.56, 0.26)) + uTime * 0.82) * 0.30;
  height += sin(dot(point, vec2(-0.24, 0.52)) - uTime * 0.62) * 0.21;
  height += cos(dot(point, vec2(0.16, 0.14)) + uTime * 0.38) * 0.27;
  height += sin(length(point + vec2(9.0, -6.0)) * 0.30 - uTime * 0.48) * 0.11;
  height += sin(dot(point, vec2(0.92, -0.68)) + uTime * 1.04) * 0.065;
  height += cos(dot(point, vec2(-0.78, -0.94)) - uTime * 0.88) * 0.045;
  height *= ambientScale;

  float distanceFromCenter = length(point);
  float plume = exp(-distanceFromCenter * distanceFromCenter * 0.20) * envelope;
  float ringRadius = mix(0.25, 9.5, smoothstep(0.0, 1.0, uTransitionProgress));
  float shock = exp(-pow(distanceFromCenter - ringRadius, 2.0) * 0.62) * envelope;
  float wake = sin(distanceFromCenter * 2.7 - uTransitionProgress * 18.0)
    * exp(-distanceFromCenter * 0.15) * envelope;
  height += plume * 3.2 * uTransitionDirection;
  height += shock * (1.45 + 0.7 * uTransitionDirection);
  height += wake * 0.75;
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

  float distanceFromCenter = length(p.xy);
  float ringRadius = mix(0.25, 9.5, smoothstep(0.0, 1.0, uTransitionProgress));
  float ring = exp(-pow(distanceFromCenter - ringRadius, 2.0) * 0.72);
  float center = exp(-distanceFromCenter * distanceFromCenter * 0.24);
  vTransitionFoam = (ring * 1.25 + center * 1.15) * transitionEnvelope();

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
varying float vTransitionFoam;
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
  float eventFoam = smoothstep(0.08, 0.72, vTransitionFoam);

  vec3 lagoon = vec3(0.055, 0.44, 0.54);
  vec3 turquoise = vec3(0.10, 0.73, 0.75);
  vec3 skyReflection = mix(vec3(0.26, 0.66, 0.82), uSunColor, 0.20);
  vec3 sea = mix(lagoon, turquoise, light * 0.66 + 0.18);
  sea = mix(sea, skyReflection, fresnel * 0.74);
  sea += uSunColor * highlight * (0.72 + eventFoam);

  float sandPattern = sin(vWorldPosition.x * 0.32 + vWorldPosition.z * 0.17) * 0.5 + 0.5;
  vec3 drySand = mix(vec3(0.78, 0.48, 0.20), vec3(0.98, 0.72, 0.36), sandPattern);
  vec3 wetSand = mix(vec3(0.26, 0.46, 0.40), vec3(0.72, 0.67, 0.45), sandPattern);
  float wetness = vSandMask * (1.0 - smoothstep(0.5, 7.5, vShoreDistance)) * uWetSandStrength;
  vec3 sand = mix(drySand, wetSand, wetness);
  sand *= 0.48 + uSunIntensity * 0.60;

  vec3 color = mix(sea, sand, vSandMask);
  float allFoam = max(max(foam, eventFoam), shoreFoam * 0.76);
  color = mix(color, vec3(0.86, 0.98, 0.94), allFoam);
  float haze = smoothstep(48.0, 145.0, length(cameraPosition - vWorldPosition));
  color = mix(color, mix(vec3(0.20, 0.48, 0.62), vec3(0.72, 0.80, 0.70), vSandMask), haze * 0.72);
  gl_FragColor = vec4(color, 1.0);
}
`;

const skyVertexShader = `
varying vec3 vSkyDirection;

void main() {
  vec4 worldPosition = modelMatrix * vec4(position, 1.0);
  vSkyDirection = normalize(worldPosition.xyz - cameraPosition);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const skyFragmentShader = `
uniform vec3 uSunDirection;
uniform vec3 uSunColor;
uniform float uSunIntensity;
uniform float uSkyPhase;
uniform float uCloudOffset;
varying vec3 vSkyDirection;

float cloudNoise(vec2 point) {
  float value = 0.0;
  value += sin(point.x * 1.7 + sin(point.y * 1.2)) * 0.5;
  value += sin(point.x * 3.9 - point.y * 2.3 + 1.6) * 0.25;
  value += cos(point.x * 8.3 + point.y * 5.1) * 0.125;
  return value * 0.5 + 0.5;
}

void main() {
  vec3 direction = normalize(vSkyDirection);
  float horizon = smoothstep(-0.22, 0.58, direction.y);
  vec3 dayTop = vec3(0.06, 0.35, 0.78);
  vec3 dayHorizon = vec3(0.45, 0.78, 0.92);
  vec3 nightTop = vec3(0.015, 0.035, 0.12);
  vec3 nightHorizon = vec3(0.10, 0.16, 0.28);
  vec3 top = mix(nightTop, dayTop, 0.16 + uSunIntensity * 0.84);
  vec3 lower = mix(nightHorizon, dayHorizon, 0.10 + uSunIntensity * 0.90);
  vec3 color = mix(lower, top, horizon);

  float warm = (1.0 - smoothstep(0.08, 0.62, uSunIntensity)) * step(0.5, direction.y);
  color = mix(color, vec3(0.98, 0.48, 0.20), warm * 0.34);
  float sunDot = max(dot(direction, normalize(uSunDirection)), 0.0);
  float halo = pow(sunDot, 9.0) * (0.35 + uSunIntensity * 0.65);
  float disk = pow(sunDot, 180.0) * (0.6 + uSunIntensity * 0.4);
  color += uSunColor * (halo * 0.42 + disk * 0.85);

  vec2 cloudPoint = vec2(atan(direction.x, direction.z) * 0.62 + uCloudOffset, direction.y * 2.8 + 0.5);
  float cloud = smoothstep(0.58, 0.80, cloudNoise(cloudPoint));
  cloud *= smoothstep(-0.02, 0.42, direction.y);
  vec3 cloudColor = mix(vec3(0.48, 0.57, 0.72), vec3(1.0, 0.96, 0.84), uSunIntensity);
  color = mix(color, cloudColor, cloud * (0.18 + uSunIntensity * 0.32));

  float stars = smoothstep(0.985, 1.0, sin(direction.x * 270.0 + direction.y * 190.0) * 0.5 + 0.5);
  color += vec3(0.42, 0.56, 0.90) * stars * (1.0 - uSunIntensity) * (1.0 - smoothstep(-0.05, 0.38, direction.y));
  color *= 0.86 + uSkyPhase * 0.03;
  gl_FragColor = vec4(color, 1.0);
}
`;

const coastlineVertexShader = `
void main() {
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const coastlineFragmentShader = `
uniform vec3 uSunColor;
uniform float uSunIntensity;
uniform float uSkyPhase;

void main() {
  vec3 duskGreen = vec3(0.035, 0.16, 0.12);
  vec3 dayGreen = vec3(0.045, 0.30, 0.16);
  vec3 color = mix(duskGreen, dayGreen, uSunIntensity);
  color += uSunColor * (0.05 + uSunIntensity * 0.10);
  color *= 0.92 + uSkyPhase * 0.02;
  gl_FragColor = vec4(color, 0.98);
}
`;

const particleVertexShader = `
attribute float aAngle;
attribute float aRadius;
attribute float aHeight;
attribute float aDelay;
uniform float uParticleProgress;
uniform float uParticleStrength;
varying float vParticleAlpha;

void main() {
  float localProgress = clamp((uParticleProgress - aDelay) / max(0.12, 1.0 - aDelay), 0.0, 1.0);
  float arc = sin(localProgress * 3.14159265);
  float radialDistance = aRadius * (0.28 + localProgress * 1.45);
  vec3 p = position;
  p.x += cos(aAngle) * radialDistance;
  p.z += sin(aAngle) * radialDistance;
  p.y += arc * aHeight + 0.12 - localProgress * localProgress * 0.65;
  vec4 viewPosition = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * viewPosition;
  gl_PointSize = (5.0 + aHeight * 2.4) * uParticleStrength * min(2.2, 16.0 / max(1.0, -viewPosition.z));
  vParticleAlpha = arc * uParticleStrength * step(aDelay, uParticleProgress);
}
`;

const particleFragmentShader = `
varying float vParticleAlpha;

void main() {
  vec2 center = gl_PointCoord - vec2(0.5);
  float distanceToCenter = length(center);
  if (distanceToCenter > 0.5) discard;
  float edge = smoothstep(0.5, 0.12, distanceToCenter);
  gl_FragColor = vec4(0.86, 1.0, 0.98, edge * vParticleAlpha);
}
`;

export interface OceanTransition {
  token: string;
  phase: "emerge" | "switch" | "sink";
}

export interface BeachEnvironment {
  locationEnabled: boolean;
  timeZone: string;
  coordinates: GeoPoint | null;
}

interface OceanSceneProps {
  transition?: OceanTransition | null;
  effectsEnabled?: boolean;
  environment?: BeachEnvironment;
  onFallbackChange?: (fallback: boolean) => void;
}

interface TransitionUniforms {
  progress: { value: number };
  strength: { value: number };
  direction: { value: number };
  particleProgress: { value: number };
  particleStrength: { value: number };
}

interface SharedUniforms {
  [uniform: string]: { value: unknown };
  uTime: { value: number };
  uSunDirection: { value: THREE.Vector3 };
  uSunColor: { value: THREE.Vector3 };
  uSunIntensity: { value: number };
  uSkyPhase: { value: number };
  uCloudOffset: { value: number };
  uShorelineOffset: { value: number };
  uWetSandStrength: { value: number };
}

interface RenderQuality {
  surfaceX: number;
  surfaceY: number;
  particleCount: number;
  dpr: number;
  skyWidthSegments: number;
  skyHeightSegments: number;
  palmCount: number;
}

const phaseConfig = {
  emerge: { duration: 1050, strength: 1, direction: 1, particles: 1 },
  switch: { duration: 620, strength: 0.58, direction: 0.35, particles: 0.5 },
  sink: { duration: 900, strength: 0.82, direction: -1, particles: 0.24 },
} as const;

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
      particleCount: 96,
      dpr: 1.25,
      skyWidthSegments: 20,
      skyHeightSegments: 12,
      palmCount: 4,
    };
  }
  return {
    surfaceX: 180,
    surfaceY: 140,
    particleCount: 180,
    dpr: Math.min(window.devicePixelRatio || 1, 1.6),
    skyWidthSegments: 28,
    skyHeightSegments: 16,
    palmCount: 6,
  };
}

function isLowPowerDevice(): boolean {
  const hints = navigator as Navigator & { deviceMemory?: number };
  return (navigator.hardwareConcurrency ?? 4) <= 2 || (hints.deviceMemory ?? 4) <= 2;
}

function phaseCode(phase: SunPhase): number {
  return phase === "night" ? 0 : phase === "dawn" ? 1 : phase === "day" ? 2 : 3;
}

function createCoastlineGeometry(palmCount: number): THREE.BufferGeometry {
  const vertices: number[] = [];
  const addTriangle = (a: [number, number, number], b: [number, number, number], c: [number, number, number]) => {
    vertices.push(...a, ...b, ...c);
  };
  const bottom = -18;
  const depth = -86;
  const points = 22;
  for (let index = 0; index < points - 1; index += 1) {
    const x0 = -150 + index * (300 / (points - 1));
    const x1 = -150 + (index + 1) * (300 / (points - 1));
    const h0 = 2.8 + Math.sin(index * 0.78) * 1.5 + Math.cos(index * 0.32) * 0.9;
    const h1 = 2.8 + Math.sin((index + 1) * 0.78) * 1.5 + Math.cos((index + 1) * 0.32) * 0.9;
    addTriangle([x0, bottom, depth], [x1, bottom, depth], [x1, h1, depth]);
    addTriangle([x0, bottom, depth], [x1, h1, depth], [x0, h0, depth]);
  }
  for (let index = 0; index < palmCount; index += 1) {
    const x = 46 + index * 9.2;
    const height = 6.0 + (index % 3) * 1.3;
    const y = 3.2 + Math.sin(index * 1.3) * 0.7;
    const trunkWidth = 0.18;
    addTriangle([x - trunkWidth, y, depth - 0.3], [x + trunkWidth, y, depth - 0.3], [x + trunkWidth * 0.8, y + height, depth - 0.3]);
    addTriangle([x - trunkWidth, y, depth - 0.3], [x + trunkWidth * 0.8, y + height, depth - 0.3], [x - trunkWidth * 0.8, y + height, depth - 0.3]);
    const crownY = y + height;
    for (let leaf = 0; leaf < 7; leaf += 1) {
      const angle = (leaf / 7) * Math.PI * 2;
      const length = 1.1 + (leaf % 2) * 0.35;
      const tip: [number, number, number] = [x + Math.cos(angle) * length, crownY + Math.sin(angle) * 0.42, depth - 0.3];
      const side: [number, number, number] = [x + Math.cos(angle + 0.5) * 0.22, crownY + Math.sin(angle + 0.5) * 0.18, depth - 0.3];
      const other: [number, number, number] = [x + Math.cos(angle - 0.5) * 0.22, crownY + Math.sin(angle - 0.5) * 0.18, depth - 0.3];
      addTriangle(side, tip, other);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(vertices), 3));
  return geometry;
}

function normalizeEnvironment(environment?: BeachEnvironment): BeachEnvironment {
  if (!environment) return DEFAULT_ENVIRONMENT;
  return {
    locationEnabled: Boolean(environment.locationEnabled && environment.coordinates),
    timeZone: environment.timeZone || DEFAULT_TIME_ZONE,
    coordinates: environment.coordinates,
  };
}

export default function OceanScene({
  transition = null,
  effectsEnabled = true,
  environment,
  onFallbackChange,
}: OceanSceneProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);
  const [reducedMotion, setReducedMotion] = useState(() => (
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ));
  const startedAtRef = useRef<number | null>(null);
  const transitionKeyRef = useRef<string | null>(null);
  const activePhaseRef = useRef<OceanTransition["phase"]>("emerge");
  const uniformsRef = useRef<TransitionUniforms | null>(null);
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
      onFallbackChange?.(true);
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
      onFallbackChange?.(true);
      return;
    }

    const quality = qualityForDevice();
    onFallbackChange?.(false);
    setFallback(false);
    configureRenderer(renderer);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 400);
    camera.position.set(0, 8.4, 13.5);
    camera.lookAt(0, 0, -3);
    const sharedUniforms: SharedUniforms = {
      uTime: { value: 0 },
      uSunDirection: { value: new THREE.Vector3(-0.4, 0.85, -0.3) },
      uSunColor: { value: new THREE.Vector3(1.0, 0.82, 0.52) },
      uSunIntensity: { value: 0.8 },
      uSkyPhase: { value: 2 },
      uCloudOffset: { value: 0 },
      uShorelineOffset: { value: 0 },
      uWetSandStrength: { value: 0.88 },
    };

    const surfaceGeometry = new THREE.PlaneGeometry(320, 320, quality.surfaceX, quality.surfaceY);
    const surfaceUniforms = {
      ...sharedUniforms,
      uTransitionProgress: { value: 0 },
      uTransitionStrength: { value: 0 },
      uTransitionDirection: { value: 0 },
    };
    const surfaceMaterial = new THREE.ShaderMaterial({
      uniforms: surfaceUniforms,
      vertexShader: surfaceVertexShader,
      fragmentShader: surfaceFragmentShader,
      side: THREE.DoubleSide,
    });
    const ocean = new THREE.Mesh(surfaceGeometry, surfaceMaterial);
    ocean.rotation.x = -Math.PI / 2;

    const particleCount = quality.particleCount;
    const particleGeometry = new THREE.BufferGeometry();
    const positions = new Float32Array(particleCount * 3);
    const angles = new Float32Array(particleCount);
    const radii = new Float32Array(particleCount);
    const heights = new Float32Array(particleCount);
    const delays = new Float32Array(particleCount);
    for (let index = 0; index < particleCount; index += 1) {
      const fraction = index / particleCount;
      angles[index] = fraction * Math.PI * 2 * 7.0;
      radii[index] = 1.0 + ((index * 37) % 97) / 97 * 7.0;
      heights[index] = 2.2 + ((index * 53) % 89) / 89 * 6.0;
      delays[index] = ((index * 29) % 61) / 61 * 0.28;
    }
    particleGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    particleGeometry.setAttribute("aAngle", new THREE.BufferAttribute(angles, 1));
    particleGeometry.setAttribute("aRadius", new THREE.BufferAttribute(radii, 1));
    particleGeometry.setAttribute("aHeight", new THREE.BufferAttribute(heights, 1));
    particleGeometry.setAttribute("aDelay", new THREE.BufferAttribute(delays, 1));
    const particleUniforms = {
      uParticleProgress: { value: 0 },
      uParticleStrength: { value: 0 },
    };
    const particleMaterial = new THREE.ShaderMaterial({
      uniforms: particleUniforms,
      vertexShader: particleVertexShader,
      fragmentShader: particleFragmentShader,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    const droplets = new THREE.Points(particleGeometry, particleMaterial);
    droplets.position.set(0, 0.05, 0);

    const skyGeometry = new THREE.SphereGeometry(230, quality.skyWidthSegments, quality.skyHeightSegments);
    const skyMaterial = new THREE.ShaderMaterial({
      uniforms: sharedUniforms,
      vertexShader: skyVertexShader,
      fragmentShader: skyFragmentShader,
      side: THREE.BackSide,
      depthWrite: false,
      depthTest: false,
    });
    const sky = new THREE.Mesh(skyGeometry, skyMaterial);
    sky.renderOrder = -10;

    const coastlineGeometry = createCoastlineGeometry(quality.palmCount);
    const coastlineMaterial = new THREE.ShaderMaterial({
      uniforms: sharedUniforms,
      vertexShader: coastlineVertexShader,
      fragmentShader: coastlineFragmentShader,
      transparent: true,
      depthWrite: false,
      depthTest: false,
    });
    const coastline = new THREE.Mesh(coastlineGeometry, coastlineMaterial);
    coastline.renderOrder = -5;
    scene.add(sky);
    scene.add(coastline);
    scene.add(ocean);
    scene.add(droplets);

    uniformsRef.current = {
      progress: surfaceUniforms.uTransitionProgress,
      strength: surfaceUniforms.uTransitionStrength,
      direction: surfaceUniforms.uTransitionDirection,
      particleProgress: particleUniforms.uParticleProgress,
      particleStrength: particleUniforms.uParticleStrength,
    };

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
      sharedUniforms.uSkyPhase.value = phaseCode(sun.phase);
      lastSunMinute = Math.floor(current.getTime() / 60_000);
      lastEnvironmentKey = `${environmentValue.locationEnabled}:${environmentValue.timeZone}:${environmentValue.coordinates?.latitude ?? ""}:${environmentValue.coordinates?.longitude ?? ""}`;
    };
    const resetTransition = () => {
      surfaceUniforms.uTransitionProgress.value = 0;
      surfaceUniforms.uTransitionStrength.value = 0;
      surfaceUniforms.uTransitionDirection.value = 0;
      particleUniforms.uParticleProgress.value = 0;
      particleUniforms.uParticleStrength.value = 0;
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
      sharedUniforms.uCloudOffset.value += delta * 0.003;
      sharedUniforms.uShorelineOffset.value = Math.sin(sharedUniforms.uTime.value * 0.08) * 0.65;
      sharedUniforms.uWetSandStrength.value = 0.84 + Math.sin(sharedUniforms.uTime.value * 0.08) * 0.08;
      sharedUniforms.uSunDirection.value.lerp(targetSunDirection, Math.min(1, delta * 1.8));
      sharedUniforms.uSunColor.value.lerp(targetSunColor, Math.min(1, delta * 1.8));
      sharedUniforms.uSunIntensity.value += (targetSunIntensity - sharedUniforms.uSunIntensity.value) * Math.min(1, delta * 1.8);
      if (startedAtRef.current !== null) {
        const config = phaseConfig[activePhaseRef.current];
        const progress = Math.min(1, (now - startedAtRef.current) / config.duration);
        surfaceUniforms.uTransitionProgress.value = progress;
        particleUniforms.uParticleProgress.value = progress;
        if (progress >= 1) {
          startedAtRef.current = null;
          resetTransition();
        }
      }
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
      onFallbackChange?.(true);
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
        onFallbackChange?.(true);
        return;
      }
      contextLostState = false;
      visible = !document.hidden;
      lastTime = performance.now();
      updateSunTarget();
      resize();
      setFallback(false);
      onFallbackChange?.(false);
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
      particleGeometry.dispose();
      particleMaterial.dispose();
      skyGeometry.dispose();
      skyMaterial.dispose();
      coastlineGeometry.dispose();
      coastlineMaterial.dispose();
      renderer.dispose();
      uniformsRef.current = null;
    };
  }, [onFallbackChange, reducedMotion]);

  useEffect(() => {
    const uniforms = uniformsRef.current;
    if (!effectsEnabled || !transition) {
      startedAtRef.current = null;
      transitionKeyRef.current = null;
      if (uniforms) {
        uniforms.progress.value = 0;
        uniforms.strength.value = 0;
        uniforms.direction.value = 0;
        uniforms.particleProgress.value = 0;
        uniforms.particleStrength.value = 0;
      }
      return;
    }
    const key = `${transition.token}:${transition.phase}`;
    if (key === transitionKeyRef.current) return;
    transitionKeyRef.current = key;
    activePhaseRef.current = transition.phase;
    startedAtRef.current = performance.now();
    const config = phaseConfig[transition.phase];
    if (uniforms) {
      uniforms.progress.value = 0;
      uniforms.strength.value = config.strength;
      uniforms.direction.value = config.direction;
      uniforms.particleProgress.value = 0;
      uniforms.particleStrength.value = config.particles;
    }
  }, [effectsEnabled, transition]);

  return (
    <div className={`ocean-scene${fallback ? " ocean-scene-fallback" : ""}`} aria-hidden="true">
      <canvas ref={canvasRef} className="ocean-canvas" />
      <div className="ocean-haze" />
    </div>
  );
}
