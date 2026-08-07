import { Anchor, Timeline, Tooltip } from "antd";
import type { AnchorLinkItemProps } from "antd/es/anchor/Anchor";
import { useMemo, type RefObject } from "react";
import type { ChatMessage } from "../../types";

export interface ConversationTurn {
  userMessage: ChatMessage;
  assistantMessages: ChatMessage[];
}

export interface ConversationTimelineProps {
  messages: readonly ChatMessage[];
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}

export function buildConversationTurns(messages: readonly ChatMessage[]): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  let current: ConversationTurn | undefined;

  for (const message of messages) {
    if (message.role === "user") {
      current = { userMessage: message, assistantMessages: [] };
      turns.push(current);
    } else if (current) {
      current.assistantMessages.push(message);
    }
  }

  return turns;
}

export function conversationTurnId(messageId: string): string {
  return `chat-turn-${encodeURIComponent(messageId)}`;
}

function tooltipContent(content: string) {
  return <span className="conversation-timeline-tooltip">{content || "（空消息）"}</span>;
}

export default function ConversationTimeline({ messages, scrollContainerRef }: ConversationTimelineProps) {
  const turns = useMemo(() => buildConversationTurns(messages), [messages]);

  if (turns.length === 0) return null;

  const timelineItems = turns.map((turn) => ({
    key: turn.userMessage.id,
    icon: <span className="conversation-timeline-dot" aria-hidden="true" />,
    content: <span className="conversation-timeline-spacer" aria-hidden="true" />,
  }));

  const anchorItems: AnchorLinkItemProps[] = turns.map((turn, index) => {
    const label = `跳转到第 ${index + 1} 轮对话：${turn.userMessage.content || "空消息"}`;
    return {
      key: turn.userMessage.id,
      href: `#${conversationTurnId(turn.userMessage.id)}`,
      title: (
        <Tooltip placement="left" title={tooltipContent(turn.userMessage.content)}>
          <span className="conversation-timeline-hitbox" aria-label={label} />
        </Tooltip>
      ),
    };
  });

  return (
    <aside className="conversation-timeline" aria-label="对话轮次导航">
      <Timeline className="conversation-timeline-visual" items={timelineItems} />
      <Anchor
        className="conversation-timeline-anchor"
        affix={false}
        getContainer={() => scrollContainerRef.current ?? window}
        items={anchorItems}
        targetOffset={32}
        onClick={(event) => event.preventDefault()}
      />
    </aside>
  );
}
