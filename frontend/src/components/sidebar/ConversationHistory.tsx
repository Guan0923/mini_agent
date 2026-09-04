import { DeleteOutlined, EditOutlined, InboxOutlined, LoadingOutlined, MessageOutlined, MoreOutlined } from "@ant-design/icons";
import { App as AntApp, Button, Dropdown, Form, Input, Modal, Spin, type MenuProps } from "antd";
import { useState, type CSSProperties } from "react";
import { useSortable } from "@dnd-kit/sortable";
import type { Conversation } from "../../types";
import type { HistoryMutationProps } from "./types";
import { useHorizontalOverflow } from "./useHorizontalOverflow";

export function confirmDelete(
  title: string,
  onDelete: () => void | Promise<void>,
  confirm: typeof Modal.confirm = Modal.confirm,
) {
  confirm({
    title: `删除“${title || "新对话"}”？`,
    content: "删除后将从界面隐藏，但后台仍保留审计数据。确定继续吗？",
    okText: "删除",
    cancelText: "取消",
    okButtonProps: { danger: true },
    onOk: onDelete,
  });
}

interface HistoryActionsProps extends HistoryMutationProps {
  conversation: Conversation;
}

function HistoryActions({ conversation, onRename, onArchive, onDelete }: HistoryActionsProps) {
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameSaving, setRenameSaving] = useState(false);
  const [form] = Form.useForm<{ title: string }>();
  const { modal } = AntApp.useApp();
  const confirmModal = modal && typeof modal.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;
  const title = conversation.title || "新对话";
  const busy = conversation.messages.some((message) => message.running);

  function closeRename() {
    if (renameSaving) return;
    setRenameOpen(false);
    form.resetFields();
  }

  async function submitRename(values: { title: string }) {
    const next = values.title.trim();
    if (!next) {
      form.setFields([{ name: "title", errors: ["请输入会话标题"] }]);
      return;
    }
    setRenameSaving(true);
    try {
      await onRename(conversation.id, next);
      setRenameOpen(false);
      form.resetFields();
    } catch {
      // The parent renders the mutation error; leave the editor open for correction.
    } finally {
      setRenameSaving(false);
    }
  }

  const menu: MenuProps = {
    items: [
      { key: "rename", label: "重命名", icon: <EditOutlined /> },
      { type: "divider" },
      { key: "archive", label: "归档", icon: <InboxOutlined />, disabled: busy },
      { key: "delete", label: "删除", icon: <DeleteOutlined />, danger: true, disabled: busy },
    ],
    onClick: ({ key }) => {
      if (key === "rename") {
        form.setFieldsValue({ title: conversation.title || "" });
        setRenameOpen(true);
      } else if (key === "archive") {
        void onArchive(conversation.id);
      } else if (key === "delete") {
        confirmDelete(title, () => onDelete(conversation.id), confirmModal);
      }
    },
  };

  return (
    <>
      <Dropdown menu={menu} trigger={["click"]} placement="bottomRight">
        <Button
          className="icon-action"
          type="text"
          size="small"
          icon={<MoreOutlined />}
          aria-label={`更多操作：${title}`}
          aria-haspopup="menu"
        />
      </Dropdown>
      <Modal
        title="重命名"
        open={renameOpen}
        onCancel={closeRename}
        okText="保存"
        cancelText="取消"
        confirmLoading={renameSaving}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={(values) => void submitRename(values)}>
          <Form.Item
            name="title"
            label="新标题"
            rules={[{ required: true, whitespace: true, message: "请输入会话标题" }]}
          >
            <Input aria-label="新标题" autoFocus maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

const historyDateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function formatHistoryUpdatedAt(value?: string): string {
  if (!value) return "";
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? "" : historyDateFormatter.format(timestamp);
}

interface HistoryRowProps extends HistoryMutationProps {
  conversation: Conversation;
  selected: boolean;
  onSelect: (id: string) => void;
  dragDisabled?: boolean;
}

export function HistoryRow({ conversation, selected, onSelect, onRename, onArchive, onDelete, dragDisabled = false }: HistoryRowProps) {
  const title = conversation.title || "新对话";
  const running = conversation.messages.some((message) => message.running);
  const messageCount = conversation.messageCount
    ?? conversation.messages.filter((message) => message.role === "user" || message.role === "assistant").length;
  const updatedAt = formatHistoryUpdatedAt(conversation.updatedAt);
  const meta = `${messageCount} 条消息${updatedAt ? ` · ${updatedAt}` : ""}`;
  const { viewportRef, textRef, overflow, measure } = useHorizontalOverflow(title);
  const [scrolling, setScrolling] = useState(false);
  const textStyle = { "--history-shift": `-${overflow}px` } as CSSProperties;
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: conversation.id,
    disabled: dragDisabled,
  });
  const rowStyle: CSSProperties = {
    padding: "2px 0",
    border: 0,
    transform: transform ? `translate3d(0, ${Math.round(transform.y)}px, 0)` : undefined,
    transition,
    zIndex: isDragging ? 2 : undefined,
  };

  return (
    <li
      ref={setNodeRef}
      className={`history-list-item${isDragging ? " is-dragging" : ""}`}
      style={rowStyle}
      {...attributes}
      {...listeners}
      role="listitem"
      tabIndex={dragDisabled ? -1 : 0}
      aria-label={`拖动排序：${title}`}
      aria-roledescription="可排序对话"
      onMouseEnter={() => setScrolling(measure() > 1)}
      onMouseLeave={() => setScrolling(false)}
    >
      <div className="history-item">
        <Button
          className={`history-entry-button${selected ? " selected" : ""}`}
          type="text"
          title={title}
          onClick={() => onSelect(conversation.id)}
          aria-label={title}
          aria-current={selected ? "page" : undefined}
        >
          <span className="history-entry-icon">
            {running ? (
              <span role="status" aria-label="正在运行" title="正在运行">
                <Spin size="small" indicator={<LoadingOutlined spin />} />
              </span>
            ) : (
              <MessageOutlined aria-hidden="true" />
            )}
          </span>
          <span className="history-entry-copy">
            <span className={`history-summary-viewport${scrolling ? " is-scrolling" : ""}`} ref={viewportRef}>
              <span className="history-summary-text" ref={textRef} style={textStyle}>{title}</span>
            </span>
            <span className="history-meta">{meta}</span>
          </span>
        </Button>
        <div
          className="history-actions"
          onMouseDown={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
          onTouchStart={(event) => event.stopPropagation()}
        >
          <HistoryActions conversation={conversation} onRename={onRename} onArchive={onArchive} onDelete={onDelete} />
        </div>
      </div>
    </li>
  );
}
