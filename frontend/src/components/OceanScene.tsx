import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

const surfaceVertexShader = `
uniform float uTime;
uniform float uTransitionProgress;
uniform float uTransitionStrength;
uniform float uTransitionDirection;
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;
varying float vTransitionFoam;

float transitionEnvelope() {
  return sin(clamp(uTransitionProgress, 0.0, 1.0) * 3.14159265) * uTransitionStrength;
}

float waveHeight(vec2 point) {
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

void main() {
  vec3 p = position;
  float stepSize = 0.16;
  float height = waveHeight(p.xy);
  float heightX = waveHeight(p.xy + vec2(stepSize, 0.0));
  float heightY = waveHeight(p.xy + vec2(0.0, stepSize));
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
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}
`;

const surfaceFragmentShader = `
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;
varying float vTransitionFoam;

void main() {
  vec3 normal = normalize(vWorldNormal);
  vec3 viewDirection = normalize(cameraPosition - vWorldPosition);
  vec3 lightDirection = normalize(vec3(-0.45, 0.82, 0.30));
  vec3 halfDirection = normalize(lightDirection + viewDirection);
  float light = max(dot(normal, lightDirection), 0.0);
  float fresnel = pow(1.0 - max(dot(normal, viewDirection), 0.0), 3.2);
  float highlight = pow(max(dot(normal, halfDirection), 0.0), 64.0);
  float crest = smoothstep(0.30, 0.58, vElevation);
  float foamPattern = sin(vWorldPosition.x * 0.72 + vWorldPosition.z * 0.31) * 0.5 + 0.5;
  float foam = crest * smoothstep(0.42, 0.78, foamPattern) * 0.38;
  float eventFoam = smoothstep(0.08, 0.72, vTransitionFoam);

  vec3 lagoon = vec3(0.10, 0.62, 0.68);
  vec3 turquoise = vec3(0.20, 0.78, 0.75);
  vec3 skyReflection = vec3(0.63, 0.88, 0.94);
  vec3 sea = mix(lagoon, turquoise, light * 0.66 + 0.18);
  sea = mix(sea, skyReflection, fresnel * 0.74);
  sea += vec3(0.82, 0.98, 0.94) * highlight * (0.72 + eventFoam);
  sea = mix(sea, vec3(0.88, 1.0, 0.97), max(foam, eventFoam));
  float haze = smoothstep(48.0, 145.0, length(cameraPosition - vWorldPosition));
  sea = mix(sea, vec3(0.58, 0.85, 0.88), haze * 0.78);
  gl_FragColor = vec4(sea, 1.0);
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

interface OceanSceneProps {
  transition?: OceanTransition | null;
  effectsEnabled?: boolean;
  onFallbackChange?: (fallback: boolean) => void;
}

interface TransitionUniforms {
  progress: { value: number };
  strength: { value: number };
  direction: { value: number };
  particleProgress: { value: number };
  particleStrength: { value: number };
}

const phaseConfig = {
  emerge: { duration: 1050, strength: 1, direction: 1, particles: 1 },
  switch: { duration: 620, strength: 0.58, direction: 0.35, particles: 0.5 },
  sink: { duration: 900, strength: 0.82, direction: -1, particles: 0.24 },
} as const;

export default function OceanScene({ transition = null, effectsEnabled = true, onFallbackChange }: OceanSceneProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);
  const startedAtRef = useRef<number | null>(null);
  const transitionKeyRef = useRef<string | null>(null);
  const activePhaseRef = useRef<OceanTransition["phase"]>("emerge");
  const uniformsRef = useRef<TransitionUniforms | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const hints = navigator as Navigator & { deviceMemory?: number };
    const lowPowerDevice = (navigator.hardwareConcurrency ?? 4) <= 2 || (hints.deviceMemory ?? 4) <= 2;
    if (reducedMotion || lowPowerDevice) {
      setFallback(true);
      onFallbackChange?.(true);
      return;
    }

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
    } catch {
      setFallback(true);
      onFallbackChange?.(true);
      return;
    }

    onFallbackChange?.(false);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
    renderer.setClearColor(0xbcefeb, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 400);
    camera.position.set(0, 8.4, 13.5);
    camera.lookAt(0, 0, -3);

    const surfaceGeometry = new THREE.PlaneGeometry(320, 320, 180, 140);
    const surfaceUniforms = {
      uTime: { value: 0 },
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
    scene.add(ocean);

    const dropletCount = 180;
    const particleGeometry = new THREE.BufferGeometry();
    const positions = new Float32Array(dropletCount * 3);
    const angles = new Float32Array(dropletCount);
    const radii = new Float32Array(dropletCount);
    const heights = new Float32Array(dropletCount);
    const delays = new Float32Array(dropletCount);
    for (let index = 0; index < dropletCount; index += 1) {
      const fraction = index / dropletCount;
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
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
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
      surfaceUniforms.uTime.value += delta;
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

    resize();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", visibility);
    canvas.addEventListener("webglcontextlost", contextLost, { passive: false });
    schedule();

    return () => {
      contextLostState = true;
      if (frame !== null) cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", visibility);
      canvas.removeEventListener("webglcontextlost", contextLost);
      surfaceGeometry.dispose();
      surfaceMaterial.dispose();
      particleGeometry.dispose();
      particleMaterial.dispose();
      renderer.dispose();
      uniformsRef.current = null;
    };
  }, []);

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
