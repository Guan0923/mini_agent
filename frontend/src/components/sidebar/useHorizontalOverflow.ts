import { useCallback, useEffect, useRef, useState } from "react";

export function useHorizontalOverflow(value: string) {
  const viewportRef = useRef<HTMLSpanElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  const measure = useCallback((): number => {
    const viewport = viewportRef.current;
    const text = textRef.current;
    if (!viewport || !text) return 0;
    const next = Math.max(0, text.scrollWidth - viewport.clientWidth);
    const measured = next > 1 ? next : 0;
    setOverflow(measured);
    return measured;
  }, []);

  useEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    if (observer && viewportRef.current) observer.observe(viewportRef.current);
    if (observer && textRef.current) observer.observe(textRef.current);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [measure, value]);

  return { viewportRef, textRef, overflow, measure };
}
