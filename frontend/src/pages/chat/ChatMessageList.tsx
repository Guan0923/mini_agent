import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import { Button, Input } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import type { MouseEvent as ReactMouseEvent, RefObject, UIEventHandler } from "react";
import MarkdownContent from "../../components/MarkdownContent";
import ShimmerText from "../../components/ShimmerText";
import type { ChatMessage, DecisionRequest, DisplayMode } from "../../types";
import ConversationTimeline, { conversationTurnId } from "./ConversationTimeline";
import { AssistantMessage, MessageActions, MessageReferenceChip } from "./messageParts";

interface ChatMessageListProps {
  messages: ChatMessage[];
  sessionId?: string;
  display: DisplayMode;
  interactionBusy: boolean;
  isMobile: boolean;
  compactionPending: boolean;
  chatScrollRef: RefObject<HTMLDivElement>;
  onScroll: UIEventHandler<HTMLDivElement>;
  editingMessageId: string | null;
  editingDraft: string;
  editRef: RefObject<TextAreaRef>;
  rewindPending: boolean;
  editingSubmitting: boolean;
  canEdit: boolean;
  setEditingDraft: (value: string) => void;
  cancelEdit: () => void;
  saveEdit: (message: ChatMessage) => Promise<void>;
  beginEdit: (message: ChatMessage) => void;
  handleUserBubbleClick: (event: ReactMouseEvent<HTMLDivElement>, message: ChatMessage) => void;
  messageVersion: (message: ChatMessage) => { index: number; total: number } | undefined;
  changeMessageVersion: (message: ChatMessage, direction: -1 | 1) => Promise<void>;
  onDecision: (request: DecisionRequest, choice: string, options?: { supplement?: string; answers?: Record<string, string[]> }) => Promise<void>;
  onFork?: (messageId: string) => void;
  sandboxFailure?: string | null;
}

export function ChatMessageList({
  messages,
  sessionId,
  display,
  interactionBusy,
  isMobile,
  compactionPending,
  chatScrollRef,
  onScroll,
  editingMessageId,
  editingDraft,
  editRef,
  rewindPending,
  editingSubmitting,
  canEdit,
  setEditingDraft,
  cancelEdit,
  saveEdit,
  beginEdit,
  handleUserBubbleClick,
  messageVersion,
  changeMessageVersion,
  onDecision,
  onFork,
  sandboxFailure,
}: ChatMessageListProps) {
  return (
    <div className="chat-scroll" ref={chatScrollRef} data-conversation-scroll onScroll={onScroll}>
      <div className="chat-scroll-content">
        <div className="chat-messages">
          {messages.length === 0 ? (
            <div className="welcome">
              <div className="logo">Mini-Agent</div>
              <p className="welcome-sub">向你的智能体提问，它会调用文件、Shell、Web 等工具完成任务</p>
            </div>
          ) : messages.map((message) => message.role === "user" ? (
            <div className={`message user${message.pending ? " is-pending" : ""}`} id={conversationTurnId(message.id)} data-chat-anchor-key={message.id} key={message.id}>
              <div className={editingMessageId === message.id ? "message-content is-editing" : "message-content"}>
                {editingMessageId === message.id ? (
                  <div className="message-edit" aria-label="编辑用户消息">
                    <Input.TextArea
                      className="message-edit-input"
                      ref={editRef}
                      aria-label="编辑用户消息"
                      value={editingDraft}
                      disabled={interactionBusy}
                      onChange={(event) => setEditingDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.preventDefault();
                          cancelEdit();
                        } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                          event.preventDefault();
                          void saveEdit(message);
                        }
                      }}
                      autoSize={{ minRows: 2, maxRows: 8 }}
                    />
                    <div className="message-edit-actions">
                      <Button type="text" onClick={cancelEdit}>取消</Button>
                      <Button
                        type="primary"
                        onClick={() => void saveEdit(message)}
                        loading={rewindPending || editingSubmitting}
                        disabled={!editingDraft.trim() || editingSubmitting || rewindPending || interactionBusy}
                      >保存并重新生成</Button>
                    </div>
                  </div>
                ) : (
                  <div
                    className="bubble user-bubble"
                    onClick={(event) => handleUserBubbleClick(event, message)}
                    title={canEdit && !interactionBusy ? "点击编辑此消息" : undefined}
                  >
                    <MarkdownContent text={message.content} />
                    {message.references && message.references.length > 0 ? (
                      <div className="message-references" aria-label="消息引用">
                        {message.references.map((reference) => (
                          <MessageReferenceChip key={`${reference.source}:${reference.path}`} reference={reference} sessionId={sessionId} />
                        ))}
                      </div>
                    ) : null}
                    {message.pending ? <span className="agent-message-pending" role="status">正在交给主 Agent 转发…</span> : null}
                  </div>
                )}
                {editingMessageId !== message.id ? (
                  <MessageTools
                    message={message}
                    interactionBusy={interactionBusy}
                    canEdit={canEdit}
                    beginEdit={beginEdit}
                    messageVersion={messageVersion}
                    changeMessageVersion={changeMessageVersion}
                  />
                ) : null}
              </div>
            </div>
          ) : (
            <AssistantMessage
              key={message.id}
              msg={message}
              display={display}
              onDecision={onDecision}
              busy={interactionBusy}
              onFork={onFork && (message.status === "success" || message.status === "failed") ? () => onFork(message.id) : undefined}
            />
          ))}
          {sandboxFailure ? (
            <div className="message assistant sandbox-health-failure" role="status" aria-live="polite">
              <div className="message-content">
                <div className="bubble assistant-bubble">
                  <MarkdownContent text={`沙箱 Broker 不可用：${sandboxFailure}`} />
                </div>
              </div>
            </div>
          ) : null}
          {compactionPending ? (
            <div className="message assistant runtime-compaction-progress" role="status" aria-live="polite">
              <ShimmerText active>正在执行compaction操作中</ShimmerText>
            </div>
          ) : null}
        </div>
      </div>
      {!isMobile ? <ConversationTimeline messages={messages} scrollContainerRef={chatScrollRef} /> : null}
    </div>
  );
}

function MessageTools({
  message,
  interactionBusy,
  canEdit,
  beginEdit,
  messageVersion,
  changeMessageVersion,
}: {
  message: ChatMessage;
  interactionBusy: boolean;
  canEdit: boolean;
  beginEdit: (message: ChatMessage) => void;
  messageVersion: (message: ChatMessage) => { index: number; total: number } | undefined;
  changeMessageVersion: (message: ChatMessage, direction: -1 | 1) => Promise<void>;
}) {
  const version = messageVersion(message);
  return (
    <>
      <MessageActions msg={message} busy={interactionBusy} onEdit={canEdit ? () => beginEdit(message) : undefined} />
      {version ? (
        <div className="message-version-controls" aria-label="消息版本切换">
          <Button
            type="text"
            size="small"
            icon={<LeftOutlined />}
            aria-label="上一个消息版本"
            disabled={interactionBusy || version.index === 0}
            onClick={() => void changeMessageVersion(message, -1)}
          />
          <span aria-live="polite">{version.index + 1} / {version.total}</span>
          <Button
            type="text"
            size="small"
            icon={<RightOutlined />}
            aria-label="下一个消息版本"
            disabled={interactionBusy || version.index >= version.total - 1}
            onClick={() => void changeMessageVersion(message, 1)}
          />
        </div>
      ) : null}
    </>
  );
}
