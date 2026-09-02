/**
 * Desktop-only user-message timeline strip.
 *
 * This is a local port of Guan0923/dsh-message-timeline. The original
 * project is MIT licensed; this adaptation keeps its tick sizing, Gaussian
 * hover treatment, tooltip and jump behavior while reading this app's
 * transcript projection instead of provider-specific session faces.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import type { ChatMessage } from "../../types";
import css from "./TimelineTicks.module.css";

export const ROW_H = 12;
export const TICK_BASE_W = 18;
export const TICK_AMP = 24;
export const GAUSS_SIGMA = 1.2;
export const GAUSS_RADIUS = 3;
export const TIP_MAX_H = 280;

interface TimelineEntry {
  key: string;
  fingerprint: string;
  time: number;
  text: string;
}

interface StripRect {
  top: number;
  left: number;
  height: number;
}

export interface ConversationTimelineProps {
  messages: readonly ChatMessage[];
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}

export function conversationTurnId(messageId: string): string {
  return `chat-turn-${encodeURIComponent(messageId)}`;
}

export function fmtTime(ms: number): string {
  const date = new Date(ms);
  if (Number.isNaN(date.getTime()) || ms <= 0) return "";
  const pad = (value: number) => (value < 10 ? `0${value}` : String(value));
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** Match the target project's content folding for transcript text fallback. */
export function timelineTextOf(content: readonly unknown[], fallback = ""): string {
  let text = "";
  let hasOther = false;
  for (const block of content) {
    if (
      block &&
      typeof block === "object" &&
      (block as { type?: unknown }).type === "text" &&
      typeof (block as { text?: unknown }).text === "string"
    ) {
      const value = (block as { text: string }).text.trim();
      text += text === "" ? value : ` ${value}`;
    } else {
      hasOther = true;
    }
  }
  text = text.trim();
  if (text === "" && hasOther) return "[非文本内容]";
  if (text !== "" && hasOther) return `${text} …`;
  return text || fallback;
}

export function buildTimelineEntries(messages: readonly ChatMessage[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  for (const message of messages) {
    if (message.role !== "user") continue;
    entries.push({
      key: message.id,
      fingerprint: JSON.stringify([message.id, message.content, message.deliveryId ?? ""]),
      time: Number.isFinite(message.timelineTime) ? Number(message.timelineTime) : 0,
      text: message.timelineText || message.content || "",
    });
  }
  return entries;
}

function gaussWeight(distance: number): number {
  return distance > GAUSS_RADIUS ? 0 : Math.exp(-(distance * distance) / (2 * GAUSS_SIGMA * GAUSS_SIGMA));
}

function hitTarget(key: string): void {
  const target = [...document.querySelectorAll<HTMLElement>("[data-chat-anchor-key]")]
    .find((candidate) => candidate.dataset.chatAnchorKey === key);
  if (!target) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.add("conversation-timeline-hit");
  target.addEventListener("animationend", () => target.classList.remove("conversation-timeline-hit"), { once: true });
}

export default function ConversationTimeline({ messages, scrollContainerRef }: ConversationTimelineProps) {
  const entries = useMemo(() => buildTimelineEntries(messages), [messages]);
  const fingerprints = useMemo(() => entries.map((entry) => entry.fingerprint), [entries]);
  const [rect, setRect] = useState<StripRect | null>(null);
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const [tipTop, setTipTop] = useState(4);
  const aliveRef = useRef(true);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const tipRef = useRef<HTMLDivElement | null>(null);
  const previousFingerprintsRef = useRef<readonly string[] | null>(null);
  const atBottomRef = useRef(true);

  useEffect(() => {
    // React StrictMode intentionally re-runs mount effects in development.
    // Re-arm the guard on the second setup so timeline clicks keep working.
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  useEffect(() => {
    const scrollEl = scrollContainerRef.current;
    if (!scrollEl || entries.length === 0 || typeof document === "undefined") {
      setRect(null);
      return;
    }
    const owner = scrollEl.parentElement;
    if (!owner) {
      setRect(null);
      return;
    }

    const measure = () => {
      const scrollBox = scrollEl.getBoundingClientRect();
      const ownerBox = owner.getBoundingClientRect();
      if (scrollBox.width === 0 || scrollBox.height === 0) {
        setRect(null);
        return;
      }
      const height = Math.max(0, scrollBox.height - 24);
      if (height <= 0) {
        setRect(null);
        return;
      }
      setRect({
        top: scrollBox.top - ownerBox.top + 12,
        left: scrollBox.left - ownerBox.left + 8,
        height,
      });
    };

    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(scrollEl);
    observer?.observe(owner);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [entries.length, scrollContainerRef]);

  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    const previous = previousFingerprintsRef.current;
    if (!viewport) return;
    previousFingerprintsRef.current = fingerprints;

    const isTrueAppend = previous !== null
      && fingerprints.length > previous.length
      && previous.every((fingerprint, index) => fingerprints[index] === fingerprint);
    const isSameProjection = previous !== null
      && fingerprints.length === previous.length
      && previous.every((fingerprint, index) => fingerprints[index] === fingerprint);
    if (previous === null || ((isTrueAppend || isSameProjection) && atBottomRef.current)) {
      viewport.scrollTop = viewport.scrollHeight;
    }
    atBottomRef.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 2;
  }, [fingerprints, rect]);

  useEffect(() => {
    if (hoverKey !== null && !entries.some((entry) => entry.key === hoverKey)) {
      setHoverKey(null);
    }
  }, [entries, hoverKey]);

  const updateTooltipPosition = useCallback(() => {
    if (!rect || hoverKey === null) return;
    const viewport = viewportRef.current;
    const row = rowRefs.current.get(hoverKey);
    const tip = tipRef.current;
    if (!viewport || !row || !tip) return;
    const tipHeight = Math.min(tip.offsetHeight || tip.scrollHeight || TIP_MAX_H, Math.max(0, rect.height - 8));
    const tickCenter = row.offsetTop - viewport.scrollTop + ROW_H / 2;
    const minTop = 4;
    const maxTop = Math.max(minTop, rect.height - tipHeight - 4);
    const nextTop = Math.max(minTop, Math.min(tickCenter - tipHeight / 2, maxTop));
    setTipTop((current) => current === nextTop ? current : nextTop);
  }, [hoverKey, rect]);

  useLayoutEffect(() => {
    updateTooltipPosition();
  }, [entries, updateTooltipPosition]);

  if (!rect || entries.length === 0) return null;

  const hover = hoverKey === null ? -1 : entries.findIndex((entry) => entry.key === hoverKey);
  const activeEntry = hover < 0 ? undefined : entries[hover];
  const tipMax = Math.max(0, Math.min(TIP_MAX_H, rect.height - 8));

  const tickStyle = (index: number): CSSProperties => {
    const weight = hover < 0 ? 0 : gaussWeight(Math.abs(index - hover));
    const width = Math.round((TICK_BASE_W + TICK_AMP * weight) * 10) / 10;
    const base = { r: 50, g: 50, b: 50, a: 0.55 };
    const selected = { r: 0x36, g: 0x11, b: 0x15, a: 1 };
    const t = 1 - weight;
    const r = Math.round(selected.r + (base.r - selected.r) * t);
    const g = Math.round(selected.g + (base.g - selected.g) * t);
    const b = Math.round(selected.b + (base.b - selected.b) * t);
    const alpha = selected.a + (base.a - selected.a) * t;
    return { width: `${width}px`, backgroundColor: `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(2)})` };
  };

  return (
    <div
      className={css.strip}
      style={{ top: `${rect.top}px`, left: `${rect.left}px`, height: `${rect.height}px` }}
      role="navigation"
      aria-label="消息时间轴"
      onMouseLeave={() => setHoverKey(null)}
    >
      <div
        className={css.viewport}
        ref={viewportRef}
        tabIndex={0}
        role="region"
        aria-label="消息时间轴刻度"
        onScroll={() => {
          const viewport = viewportRef.current;
          if (viewport) {
            atBottomRef.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 2;
          }
          updateTooltipPosition();
        }}
      >
        <div className={css.rows}>
          {entries.map((entry, index) => (
            <button
              key={entry.key}
              ref={(node) => {
                if (node) rowRefs.current.set(entry.key, node);
                else rowRefs.current.delete(entry.key);
              }}
              type="button"
              className={css.row}
              aria-label={`跳转到消息：${entry.text || "空消息"}`}
              onMouseEnter={() => setHoverKey(entry.key)}
              onFocus={() => setHoverKey(entry.key)}
              onClick={() => {
                if (aliveRef.current) hitTarget(entry.key);
              }}
            >
              <span className={css.tick} style={tickStyle(index)} />
            </button>
          ))}
        </div>
      </div>
      {activeEntry ? (
        <div
          ref={tipRef}
          className={css.tip}
          style={{ top: `${tipTop}px`, maxHeight: `${tipMax}px` }}
          role="tooltip"
          onWheelCapture={(event) => event.stopPropagation()}
        >
          <span className={css.tipTime}>{fmtTime(activeEntry.time)}</span>
          <span className={css.tipText}>{activeEntry.text || "（空消息）"}</span>
        </div>
      ) : null}
    </div>
  );
}
