import {
  Badge,
  App as AntApp,
  Button,
  Divider,
  Dropdown,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popover,
  Space,
  Spin,
  Typography,
  type MenuProps,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  InboxOutlined,
  LoadingOutlined,
  LogoutOutlined,
  MoreOutlined,
  PlusOutlined,
  BarChartOutlined,
  MessageOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { AuthUser, Conversation, Page } from "../types";
import IconAction from "./IconAction";

interface AppSidebarProps {
  user: AuthUser | null;
  conversations: Conversation[];
  archivedCount: number;
  currentId: string | null;
  page: Page;
  onNew: () => void | Promise<unknown>;
  onSelect: (id: string) => void;
  onNavigate: (page: Page) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
  onSignOut: () => void | Promise<void>;
  onProfileUpdate?: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
  onOpenSettings?: () => void;
}

interface ProfilePopoverProps {
  user: AuthUser | null;
  onSave?: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
}

function ProfilePopover({ user, onSave }: ProfilePopoverProps) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState({ display_name: "", agent_preferences: "" });
  const label = user?.display_name?.trim() || "用户";

  useEffect(() => {
    if (!open) return;
    setDraft({
      display_name: user?.display_name ?? "",
      agent_preferences: user?.agent_preferences ?? "",
    });
    setError("");
  }, [open, user?.display_name, user?.agent_preferences]);

  async function save() {
    if (!onSave) return;
    const displayName = draft.display_name.trim();
    if (!displayName) {
      setError("用户名不能为空。");
      return;
    }
    if (displayName.length > 80) {
      setError("用户名不能超过 80 个字符。");
      return;
    }
    if (draft.agent_preferences.length > 4000) {
      setError("Agent 偏好不能超过 4000 个字符。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSave({ display_name: displayName, agent_preferences: draft.agent_preferences.trim() });
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  }

  const content = (
    <div className="profile-popover-content">
      <Input
        aria-label="用户名"
        placeholder="设置一个用户名"
        maxLength={80}
        value={draft.display_name}
        onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))}
      />
      <Input.TextArea
        aria-label="Agent 偏好"
        placeholder="例如：回答简洁，先给结论，再给关键步骤。"
        maxLength={4000}
        autoSize={{ minRows: 4, maxRows: 8 }}
        value={draft.agent_preferences}
        onChange={(event) => setDraft((current) => ({ ...current, agent_preferences: event.target.value }))}
      />
      {error ? <Typography.Text type="danger">{error}</Typography.Text> : null}
      <Space className="profile-popover-actions">
        <Button onClick={() => setOpen(false)} disabled={saving}>取消</Button>
        <Button
          type="primary"
          aria-label="保存"
          onClick={() => void save()}
          loading={saving}
        >
          保存
        </Button>
      </Space>
    </div>
  );

  return (
    <Popover
      title="个人简介"
      content={content}
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      placement="topLeft"
    >
      <Button className="profile-trigger" type="text" icon={<UserOutlined />} aria-label={`个人简介：${label}`}>
        <Typography.Text ellipsis title={label}>{label}</Typography.Text>
      </Button>
    </Popover>
  );
}

function confirmDelete(
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

interface HistoryActionsProps {
  conversation: Conversation;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
}

function HistoryActions({ conversation, onRename, onArchive, onDelete }: HistoryActionsProps) {
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameSaving, setRenameSaving] = useState(false);
  const [form] = Form.useForm<{ title: string }>();
  const { modal } = AntApp.useApp();
  const confirmModal = modal && typeof modal.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;
  const title = conversation.title || "新对话";
  const busy = conversation.messages.some((message) => message.running);

  function openRename() {
    form.setFieldsValue({ title: conversation.title || "" });
    setRenameOpen(true);
  }

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
        openRename();
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
        <IconAction
          label={`更多操作：${title}`}
          icon={<MoreOutlined />}
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

interface HistoryRowProps extends HistoryActionsProps {
  selected: boolean;
  onSelect: (id: string) => void;
}

function HistoryRow({ conversation, selected, onSelect, onRename, onArchive, onDelete }: HistoryRowProps) {
  const title = conversation.title || "新对话";
  const running = conversation.messages.some((message) => message.running);
  const messageCount = conversation.messageCount ?? conversation.messages.length;
  const updatedAt = formatHistoryUpdatedAt(conversation.updatedAt);
  const meta = `${messageCount} 条消息${updatedAt ? ` · ${updatedAt}` : ""}`;
  const viewportRef = useRef<HTMLSpanElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  function measureOverflow() {
    const viewport = viewportRef.current;
    const text = textRef.current;
    if (!viewport || !text) return;
    const next = Math.max(0, text.scrollWidth - viewport.clientWidth);
    setOverflow(next > 1 ? next : 0);
  }

  function resetOverflow() {
    setOverflow(0);
  }

  const textStyle = { "--history-shift": `-${overflow}px` } as CSSProperties;

  return (
    <List.Item className="history-list-item" style={{ padding: "2px 0", border: 0 }}>
      <div className="history-item" onMouseEnter={measureOverflow} onMouseLeave={resetOverflow}>
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
            <span className={`history-summary-viewport${overflow ? " is-scrolling" : ""}`} ref={viewportRef}>
              <span className="history-summary-text" ref={textRef} style={textStyle}>{title}</span>
            </span>
            <span className="history-meta">{meta}</span>
          </span>
        </Button>
        <div className="history-actions">
          <HistoryActions
            conversation={conversation}
            onRename={onRename}
            onArchive={onArchive}
            onDelete={onDelete}
          />
        </div>
      </div>
    </List.Item>
  );
}

export default function AppSidebar({
  user,
  conversations,
  archivedCount,
  currentId,
  page,
  onNew,
  onSelect,
  onNavigate,
  onRename,
  onArchive,
  onDelete,
  onSignOut,
  onProfileUpdate,
  onOpenSettings,
}: AppSidebarProps) {
  return (
    <div
      className="app-sidebar"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        minHeight: 0,
        padding: 16,
        background: "#fff",
      }}
    >
      <Button
        type="primary"
        size="large"
        block
        icon={<PlusOutlined />}
        onClick={() => void onNew()}
        aria-label="新建对话"
      >
        新建对话
      </Button>

      <Typography.Text type="secondary" style={{ margin: "20px 8px 8px", fontSize: 12 }}>
        对话
      </Typography.Text>
      <div style={{ minHeight: 0, flex: 1, overflowY: "auto" }}>
        <List
          size="small"
          split={false}
          dataSource={conversations}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" /> }}
          renderItem={(conversation) => {
            const selected = conversation.id === currentId && page === "chat";
            return (
              <HistoryRow
                key={conversation.id}
                conversation={conversation}
                selected={selected}
                onSelect={onSelect}
                onRename={onRename}
                onArchive={onArchive}
                onDelete={onDelete}
              />
            );
          }}
        />
      </div>

      <Divider style={{ margin: "12px 0" }} />
      <div className="sidebar-utility-links">
        <Badge className="sidebar-utility-badge" count={archivedCount} size="small" offset={[5, 0]}>
          <Button
            type={page === "trash" ? "default" : "text"}
            block
            icon={<DeleteOutlined />}
            onClick={() => onNavigate("trash")}
            aria-label={`回收站${archivedCount ? ` (${archivedCount})` : ""}`}
            style={{ textAlign: "left" }}
          >
            回收站
          </Button>
        </Badge>
        <Button
          type={page === "benchmark" ? "default" : "text"}
          block
          icon={<BarChartOutlined />}
          onClick={() => onNavigate("benchmark")}
          aria-label="Benchmark"
          style={{ textAlign: "left" }}
        >
          Benchmark
        </Button>
      </div>

      <div style={{ marginTop: 16 }}>
        <Typography.Text type="secondary" style={{ display: "block", fontSize: 12 }}>
          Mini-Agent
        </Typography.Text>
        <Space style={{ width: "100%", justifyContent: "space-between", marginTop: 8 }}>
          {onOpenSettings ? (
            <Button
              className="profile-trigger"
              type="text"
              icon={<UserOutlined />}
              onClick={onOpenSettings}
              aria-label={"个人简介：" + (user?.display_name?.trim() || "用户")}
            >
              <Typography.Text ellipsis title={user?.display_name?.trim() || "用户"}>
                {user?.display_name?.trim() || "用户"}
              </Typography.Text>
            </Button>
          ) : (
            <ProfilePopover user={user} onSave={onProfileUpdate} />
          )}
          <Button
            type="text"
            size="small"
            icon={<LogoutOutlined />}
            onClick={() => void onSignOut()}
            aria-label="退出"
          >
            退出
          </Button>
        </Space>
      </div>
    </div>
  );
}

export { confirmDelete };
