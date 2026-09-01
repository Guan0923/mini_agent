import { useEffect, useRef, useState } from "react";
import { Grid } from "antd";

export const CHAT_COMPACT_WIDTH = 700;

export function useResponsiveChatLayout() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false && (typeof window === "undefined" || window.innerWidth < 768);
  const chatPageRef = useRef<HTMLDivElement | null>(null);
  const measuredChatWidthRef = useRef<number | null>(null);
  const [compact, setCompact] = useState(isMobile);
  const compactRef = useRef(isMobile);

  useEffect(() => {
    const element = chatPageRef.current;
    if (!element) return;
    const applyWidth = (width: number) => {
      if (width > 0) measuredChatWidthRef.current = width;
      const measuredWidth = measuredChatWidthRef.current;
      const next = isMobile || (measuredWidth != null && measuredWidth < CHAT_COMPACT_WIDTH);
      if (compactRef.current === next) return;
      compactRef.current = next;
      setCompact(next);
    };
    applyWidth(element.getBoundingClientRect().width);
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver((entries) => {
      applyWidth(entries[0]?.contentRect.width ?? element.getBoundingClientRect().width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [isMobile]);

  return { chatPageRef, compact, isMobile };
}
