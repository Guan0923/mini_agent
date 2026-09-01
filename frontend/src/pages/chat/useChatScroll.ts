import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../../types";

const BOTTOM_THRESHOLD_PX = 24;

function isAtBottom(scrollContainer: HTMLDivElement): boolean {
  return scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight <= BOTTOM_THRESHOLD_PX;
}

export function useChatScroll(conversationId: string | undefined, messages: ChatMessage[]) {
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const scrollConversationIdRef = useRef<string | undefined>(undefined);
  const [isAtBottomState, setIsAtBottomState] = useState(true);

  const syncBottomState = useCallback((scrollContainer: HTMLDivElement) => {
    const nextIsAtBottom = isAtBottom(scrollContainer);
    shouldStickToBottomRef.current = nextIsAtBottom;
    setIsAtBottomState((current) => current === nextIsAtBottom ? current : nextIsAtBottom);
  }, []);

  useLayoutEffect(() => {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    const conversationChanged = scrollConversationIdRef.current !== conversationId;
    scrollConversationIdRef.current = conversationId;
    if (conversationChanged) shouldStickToBottomRef.current = true;
    if (!shouldStickToBottomRef.current) return;
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    syncBottomState(scrollContainer);
  }, [conversationId, messages, syncBottomState]);

  useEffect(() => {
    const scrollContainer = chatScrollRef.current;
    const scrollContent = scrollContainer?.querySelector<HTMLElement>(".chat-scroll-content");
    if (!scrollContainer || !scrollContent || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(() => {
      if (shouldStickToBottomRef.current) scrollContainer.scrollTop = scrollContainer.scrollHeight;
      syncBottomState(scrollContainer);
    });
    observer.observe(scrollContainer);
    observer.observe(scrollContent);
    return () => observer.disconnect();
  }, [conversationId, syncBottomState]);

  const handleScroll = useCallback(() => {
    const scrollContainer = chatScrollRef.current;
    if (scrollContainer) syncBottomState(scrollContainer);
  }, [syncBottomState]);

  const scrollToBottom = useCallback(() => {
    const scrollContainer = chatScrollRef.current;
    if (!scrollContainer) return;
    scrollContainer.scrollTo({ top: scrollContainer.scrollHeight, behavior: "smooth" });
  }, []);

  return {
    chatScrollRef,
    handleScroll,
    isAtBottom: isAtBottomState,
    scrollToBottom,
  };
}
