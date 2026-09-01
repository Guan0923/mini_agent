import { useEffect, useState } from "react";
import { listQueuedMessages } from "../api";
import type { Conversation } from "../types";
import type { QueuedMessage } from "./types";

interface UseQueuedMessagesOptions {
  current: Conversation | null;
  conversations: Conversation[];
  panelConversations: Record<string, Conversation>;
  onError: (message: string) => void;
}

export function useQueuedMessages({
  current,
  conversations,
  panelConversations,
  onError,
}: UseQueuedMessagesOptions) {
  const [queuedMessages, setQueuedMessages] = useState<Map<string, QueuedMessage[]>>(() => new Map());

  useEffect(() => {
    if (!current?.id || !current.threadId) return;
    let active = true;
    void listQueuedMessages(current.threadId)
      .then((items) => {
        if (!active) return;
        setQueuedMessages((previous) => {
          const next = new Map(previous);
          next.set(current.id, items);
          return next;
        });
      })
      .catch((error) => {
        if (active) onError(String((error as Error).message ?? error));
      });
    return () => { active = false; };
  }, [current?.id, current?.threadId]);

  function updateQueuedMessages(conversationId: string, updater: (items: QueuedMessage[]) => QueuedMessage[]) {
    setQueuedMessages((previous) => {
      const queues = new Map(previous);
      const next = updater(previous.get(conversationId) ?? []);
      if (next.length > 0) queues.set(conversationId, next);
      else queues.delete(conversationId);
      return queues;
    });
  }

  async function refreshQueuedMessages(conversationId: string): Promise<void> {
    const target = conversations.find((item) => item.id === conversationId) ?? panelConversations[conversationId];
    if (!target?.threadId) return;
    const items = await listQueuedMessages(target.threadId);
    setQueuedMessages((previous) => {
      const next = new Map(previous);
      next.set(conversationId, items);
      return next;
    });
  }

  return { queuedMessages, setQueuedMessages, updateQueuedMessages, refreshQueuedMessages };
}
