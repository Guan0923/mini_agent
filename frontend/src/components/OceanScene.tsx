import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

const vertexShader = `
uniform float uTime;
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;

float waveHeight(vec2 point) {
  float height = sin(dot(point, vec2(0.56, 0.26)) + uTime * 0.82) * 0.30;
  height += sin(dot(point, vec2(-0.24, 0.52)) - uTime * 0.62) * 0.21;
  height += cos(dot(point, vec2(0.16, 0.14)) + uTime * 0.38) * 0.27;
  height += sin(length(point + vec2(9.0, -6.0)) * 0.30 - uTime * 0.48) * 0.11;
  height += sin(dot(point, vec2(0.92, -0.68)) + uTime * 1.04) * 0.065;
  height += cos(dot(point, vec2(-0.78, -0.94)) - uTime * 0.88) * 0.045;
  return height;
}

void main() {
  vec3 p = position;
  float stepSize = 0.16;
  float height = waveHeight(p.xy);
  float heightX = waveHeight(p.xy + vec2(stepSize, 0.0));
  float heightY = waveHeight(p.xy + vec2(0.0, stepSize));
  p.z = height;

  vec3 objectNormal = normalize(vec3(height - heightX, height - heightY, stepSize));
  vec4 worldPosition = modelMatrix * vec4(p, 1.0);
  vWorldPosition = worldPosition.xyz;
  vWorldNormal = normalize(mat3(modelMatrix) * objectNormal);
  vElevation = height;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}
`;

const fragmentShader = `
varying vec3 vWorldPosition;
varying vec3 vWorldNormal;
varying float vElevation;

void main() {
  vec3 normal = normalize(vWorldNormal);
  vec3 viewDirection = normalize(cameraPosition - vWorldPosition);
  vec3 lightDirection = normalize(vec3(-0.45, 0.82, 0.30));
  vec3 halfDirection = normalize(lightDirection + viewDirection);

  float light = max(dot(normal, lightDirection), 0.0);
  float fresnel = pow(1.0 - max(dot(normal, viewDirection), 0.0), 3.2);
  float highlight = pow(max(dot(normal, halfDirection), 0.0), 74.0);
  float crest = smoothstep(0.30, 0.55, vElevation);
  float foamPattern = sin(vWorldPosition.x * 0.72 + vWorldPosition.z * 0.31) * 0.5 + 0.5;
  float foam = crest * smoothstep(0.42, 0.78, foamPattern) * 0.38;

  vec3 lagoon = vec3(0.10, 0.62, 0.68);
  vec3 turquoise = vec3(0.20, 0.78, 0.75);
  vec3 skyReflection = vec3(0.63, 0.88, 0.94);
  vec3 sea = mix(lagoon, turquoise, light * 0.66 + 0.18);
  sea = mix(sea, skyReflection, fresnel * 0.74);
  sea += vec3(0.82, 0.98, 0.94) * highlight * 0.72;
  sea = mix(sea, vec3(0.88, 1.0, 0.97), foam);

  float distanceFromCamera = length(cameraPosition - vWorldPosition);
  float haze = smoothstep(48.0, 145.0, distanceFromCamera);
  sea = mix(sea, vec3(0.58, 0.85, 0.88), haze * 0.78);
  gl_FragColor = vec4(sea, 1.0);
}
`;

export default function OceanScene() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const navigatorHints = navigator as Navigator & { deviceMemory?: number };
    const lowPowerDevice = (navigator.hardwareConcurrency ?? 4) <= 2 || (navigatorHints.deviceMemory ?? 4) <= 2;
    if (reducedMotion || lowPowerDevice) {
      setFallback(true);
      return;
    }

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "high-performance" });
    } catch {
      setFallback(true);
      return;
    }

    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
    renderer.setClearColor(0xbcefeb, 0);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 400);
    camera.position.set(0, 8.4, 13.5);
    camera.lookAt(0, 0, -3);

    const geometry = new THREE.PlaneGeometry(320, 320, 180, 140);
    const uniforms = { uTime: { value: 0 } };
    const material = new THREE.ShaderMaterial({
      uniforms,
      vertexShader,
      fragmentShader,
      side: THREE.DoubleSide,
    });
    const ocean = new THREE.Mesh(geometry, material);
    ocean.rotation.x = -Math.PI / 2;
    scene.add(ocean);

    let frame: number | null = null;
    let lastTime = performance.now();
    let visible = !document.hidden;
    let contextLostState = false;

    const resize = () => {
      const width = Math.max(1, canvas.clientWidth || window.innerWidth);
      const height = Math.max(1, canvas.clientHeight || window.innerHeight);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
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
      if (frame !== null) {
        cancelAnimationFrame(frame);
        frame = null;
      }
      setFallback(true);
    };
    const schedule = () => {
      if (frame === null && visible && !contextLostState) frame = requestAnimationFrame(render);
    };
    const render = (now: number) => {
      frame = null;
      if (!visible || contextLostState) return;
      const delta = Math.min(0.05, (now - lastTime) / 1000);
      lastTime = now;
      uniforms.uTime.value += delta;
      renderer.render(scene, camera);
      schedule();
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
      geometry.dispose();
      material.dispose();
      renderer.dispose();
    };
  }, []);

  return (
    <div className={`ocean-scene${fallback ? " ocean-scene-fallback" : ""}`} aria-hidden="true">
      <canvas ref={canvasRef} className="ocean-canvas" />
      <div className="ocean-haze" />
    </div>
  );
}
