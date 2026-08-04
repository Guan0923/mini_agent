import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

function createOceanGeometry(columns: number, rows: number): THREE.BufferGeometry {
  const positions = new Float32Array(columns * rows * 3);
  let offset = 0;
  for (let row = 0; row < rows; row += 1) {
    const z = (row / Math.max(1, rows - 1) - 0.5) * 15;
    for (let column = 0; column < columns; column += 1) {
      const x = (column / Math.max(1, columns - 1) - 0.5) * 22;
      positions[offset++] = x;
      positions[offset++] = 0;
      positions[offset++] = z;
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  return geometry;
}

const vertexShader = `
uniform float uTime;
uniform float uPointerX;
uniform float uPointerVelocity;
varying float vDepth;
varying float vWave;

void main() {
  vec3 p = position;
  float base = sin(p.x * 0.34 + uTime * 0.55) * 0.24;
  base += cos(p.z * 0.46 - uTime * 0.34) * 0.18;
  base += sin((p.x + p.z) * 0.16 + uTime * 0.28) * 0.14;

  float source = uPointerX * 11.0;
  float distanceFromSource = p.x - source;
  float envelope = exp(-abs(distanceFromSource) * 0.35) * clamp(abs(uPointerVelocity) * 1.8, 0.0, 1.0);
  float direction = sign(uPointerVelocity + 0.0001);
  float ripple = sin(distanceFromSource * 2.25 - uTime * (3.5 + abs(uPointerVelocity) * 4.0) * direction);
  p.y += base + ripple * envelope * 0.72;

  vec4 mvPosition = modelViewMatrix * vec4(p, 1.0);
  vDepth = clamp((-mvPosition.z - 3.0) / 18.0, 0.0, 1.0);
  vWave = clamp((p.y + 0.6) / 1.8, 0.0, 1.0);
  gl_PointSize = clamp(2.3 + 4.5 / max(1.0, -mvPosition.z * 0.13), 1.2, 5.5);
  gl_Position = projectionMatrix * mvPosition;
}
`;

const fragmentShader = `
varying float vDepth;
varying float vWave;

void main() {
  vec2 centered = gl_PointCoord - vec2(0.5);
  float dotShape = 1.0 - smoothstep(0.15, 0.5, length(centered));
  vec3 deep = vec3(0.015, 0.105, 0.25);
  vec3 blue = vec3(0.02, 0.33, 0.52);
  vec3 foam = vec3(0.25, 0.88, 0.91);
  vec3 color = mix(deep, blue, vDepth * 0.8 + 0.18);
  color = mix(color, foam, smoothstep(0.74, 1.0, vWave) * 0.48);
  gl_FragColor = vec4(color, dotShape * (0.18 + vDepth * 0.66));
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
      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: false, powerPreference: "high-performance" });
    } catch {
      setFallback(true);
      return;
    }

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
    camera.position.set(0, 6.8, 11.5);
    camera.lookAt(0, -0.35, 0);

    const geometry = createOceanGeometry(128, 84);
    const uniforms = {
      uTime: { value: 0 },
      uPointerX: { value: 0 },
      uPointerVelocity: { value: 0 },
    };
    const material = new THREE.ShaderMaterial({
      uniforms,
      vertexShader,
      fragmentShader,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const ocean = new THREE.Points(geometry, material);
    ocean.rotation.x = -0.12;
    scene.add(ocean);

    let frame: number | null = null;
    let lastTime = performance.now();
    let lastPointerX = window.innerWidth / 2;
    let lastPointerTime = lastTime;
    let visible = !document.hidden;
    let contextLostState = false;

    const resize = () => {
      const width = window.innerWidth;
      const height = window.innerHeight;
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.6));
      renderer.setSize(width, height, false);
      camera.aspect = width / Math.max(1, height);
      camera.updateProjectionMatrix();
    };
    const pointerMove = (event: PointerEvent) => {
      const now = performance.now();
      const elapsed = Math.max(16, now - lastPointerTime);
      const normalizedX = (event.clientX / Math.max(1, window.innerWidth) - 0.5) * 2;
      const velocity = THREE.MathUtils.clamp((event.clientX - lastPointerX) / elapsed * 3.2, -1, 1);
      uniforms.uPointerX.value = THREE.MathUtils.lerp(uniforms.uPointerX.value, normalizedX, 0.34);
      uniforms.uPointerVelocity.value = velocity;
      lastPointerX = event.clientX;
      lastPointerTime = now;
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
      uniforms.uPointerVelocity.value *= Math.pow(0.06, delta);
      renderer.render(scene, camera);
      schedule();
    };

    resize();
    window.addEventListener("resize", resize);
    window.addEventListener("pointermove", pointerMove, { passive: true });
    document.addEventListener("visibilitychange", visibility);
    canvas.addEventListener("webglcontextlost", contextLost, { passive: false });

    schedule();

    return () => {
      contextLostState = true;
      if (frame !== null) cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      window.removeEventListener("pointermove", pointerMove);
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
