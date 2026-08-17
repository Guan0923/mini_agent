/**
 * Desktop-only user-message timeline strip.
 *
 * This is a local port of Guan0923/dsh-message-timeline. The original
 * project is MIT licensed; this adaptation keeps its tick sizing, Gaussian
 * hover treatment, tooltip and jump behavior while reading this app's
 * transcript projection instead of DeepSeek Harness session faces.
 */
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import type { ChatMessage } from "../../types";
import css from "./TimelineTicks.module.css";

export const MAX_TICKS = 30;
export const ROW_H = 12;
export const TICK_BASE_W = 18;
export const TICK_AMP = 24;
export const GAUSS_SIGMA = 1.2;
export const GAUSS_RADIUS = 3;
export const TIP_MAX_H = 280;

interface TimelineEntry {
  key: string;
  seq: number;
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
  let fallbackSeq = 0;
  for (const message of messages) {
    if (message.role !== "user" || message.timelineSource === "steering") continue;
    fallbackSeq += 1;
    entries.push({
      key: message.id,
      seq: Number.isFinite(message.timelineSeq) ? Number(message.timelineSeq) : fallbackSeq,
      time: Number.isFinite(message.timelineTime) ? Number(message.timelineTime) : 0,
      text: message.timelineText || message.content || "",
    });
  }
  entries.sort((a, b) => a.seq - b.seq || a.key.localeCompare(b.key));
  return entries.slice(-MAX_TICKS);
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
  const [rect, setRect] = useState<StripRect | null>(null);
  const [hover, setHover] = useState(-1);
  const aliveRef = useRef(true);

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
    const composer = document.querySelector<HTMLElement>("[data-composer-seat]");
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
      let bottom = scrollBox.bottom - 12;
      if (composer) {
        const composerBox = composer.getBoundingClientRect();
        if (composerBox.height > 0) bottom = Math.min(bottom, composerBox.top - 12);
      }
      const height = Math.max(0, bottom - (scrollBox.top + 12));
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
    if (composer) observer?.observe(composer);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [entries.length, scrollContainerRef]);

  if (!rect || entries.length === 0) return null;

  const totalHeight = entries.length * ROW_H;
  const stripTop = rect.top + Math.max(0, (rect.height - totalHeight) / 2);
  const tipMax = Math.max(120, Math.min(TIP_MAX_H, totalHeight - 16));
  let tipTop = 0;
  if (hover >= 0) {
    const tickCenter = stripTop + (hover + 0.5) * ROW_H;
    const minTop = rect.top + 4;
    const maxTop = Math.max(minTop, rect.top + rect.height - tipMax - 4);
    tipTop = Math.max(minTop, Math.min(tickCenter - tipMax / 2, maxTop)) - stripTop;
  }

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
      style={{ top: `${stripTop}px`, left: `${rect.left}px`, height: `${totalHeight}px` }}
      aria-label="消息时间轴"
      onMouseLeave={() => setHover(-1)}
    >
      {entries.map((entry, index) => (
        <button
          key={entry.key}
          type="button"
          className={css.row}
          aria-label={`跳转到消息：${entry.text || "空消息"}`}
          onMouseEnter={() => setHover(index)}
          onFocus={() => setHover(index)}
          onClick={() => {
            if (aliveRef.current) hitTarget(entry.key);
          }}
        >
          <span className={css.tick} style={tickStyle(index)} />
          {hover === index ? (
            <span
              className={css.tip}
              style={{ top: `${tipTop}px`, maxHeight: `${tipMax}px` }}
              role="tooltip"
              onWheelCapture={(event) => event.stopPropagation()}
            >
              <span className={css.tipTime}>{fmtTime(entry.time)}</span>
              <span className={css.tipText}>{entry.text || "（空消息）"}</span>
            </span>
          ) : null}
        </button>
      ))}
    </div>
  );
}
