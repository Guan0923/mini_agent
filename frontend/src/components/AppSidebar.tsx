import {
  Badge,
  App as AntApp,
  Button,
  Collapse,
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
  FolderOpenOutlined,
  WarningOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { AuthUser, Conversation, Page } from "../types";
import type { ProjectInfo } from "../api";

interface AppSidebarProps {
  user: AuthUser | null;
  conversations: Conversation[];
  archivedCount: number;
  currentId: string | null;
  page: Page;
  onNew: () => void | Promise<unknown>;
  projects?: ProjectInfo[];
  projectsLoaded?: boolean;
  projectLoading?: boolean;
  onNewProject?: () => void | Promise<unknown>;
  onNewProjectConversation?: (projectId: string) => void | Promise<unknown>;
  onRemoveProject?: (projectId: string) => void | Promise<unknown>;
  onRenameProject?: (projectId: string, name: string) => void | Promise<unknown>;
  onChangeProjectPath?: (projectId: string) => void | Promise<unknown>;
  onRevokeSkillTrust?: (projectId: string) => void | Promise<unknown>;
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

function ProfileLabel({ label }: { label: string }) {
  const viewportRef = useRef<HTMLSpanElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);
  const [scrolling, setScrolling] = useState(false);

  function measureOverflow(): number {
    const viewport = viewportRef.current;
    const text = textRef.current;
    if (!viewport || !text) return 0;
    const next = Math.max(0, text.scrollWidth - viewport.clientWidth);
    setOverflow(next > 1 ? next : 0);
    return next;
  }

  useEffect(() => {
    setScrolling(false);
    const measure = () => {
      const next = measureOverflow();
      if (next <= 1) setScrolling(false);
    };
    measure();
    window.addEventListener("resize", measure);
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
    if (observer && viewportRef.current) observer.observe(viewportRef.current);
    if (observer && textRef.current) observer.observe(textRef.current);
    return () => {
      window.removeEventListener("resize", measure);
      observer?.disconnect();
    };
  }, [label]);

  function handleMouseEnter() {
    setScrolling(measureOverflow() > 1);
  }

  const textStyle = { "--profile-shift": `-${overflow}px` } as CSSProperties;
  return (
    <span
      className={`profile-trigger-label-viewport${scrolling ? " is-scrolling" : ""}`}
      ref={viewportRef}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setScrolling(false)}
    >
      <span className="profile-trigger-label-text" ref={textRef} style={textStyle}>{label}</span>
    </span>
  );
}

function ProfilePopover({ user, onSave }: ProfilePopoverProps) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState({ display_name: "", agent_preferences: "" });
  const label = user?.display_name?.trim() || (user?.kind === "guest" ? "游客用户" : "用户");

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
        <ProfileLabel label={label} />
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

interface HistoryRowProps extends HistoryActionsProps {
  selected: boolean;
  onSelect: (id: string) => void;
}

interface ProjectSettingsProps {
  project: ProjectInfo;
  onRenameProject?: (projectId: string, name: string) => void | Promise<unknown>;
  onChangeProjectPath?: (projectId: string) => void | Promise<unknown>;
  onConfirmRemove: (project: ProjectInfo) => void;
  onRevokeSkillTrust?: (projectId: string) => void | Promise<unknown>;
}

function ProjectSettings({ project, onRenameProject, onChangeProjectPath, onConfirmRemove, onRevokeSkillTrust }: ProjectSettingsProps) {
  const [open, setOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameSaving, setRenameSaving] = useState(false);
  const [pathSaving, setPathSaving] = useState(false);
  const [draftName, setDraftName] = useState(project.name);
  const [renameError, setRenameError] = useState("");
  const [revoking, setRevoking] = useState(false);

  useEffect(() => {
    if (!renameOpen) setDraftName(project.name);
  }, [project.name, renameOpen]);

  async function saveName() {
    const name = draftName.trim();
    if (!name) {
      setRenameError("项目名称不能为空。");
      return;
    }
    if (name.length > 120) {
      setRenameError("项目名称不能超过 120 个字符。");
      return;
    }
    setRenameSaving(true);
    setRenameError("");
    try {
      await onRenameProject?.(project.project_id, name);
      setRenameOpen(false);
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : "保存失败，请稍后重试。");
    } finally {
      setRenameSaving(false);
    }
  }

  function openPathPicker() {
    setOpen(false);
    setPathSaving(true);
    void Promise.resolve(onChangeProjectPath?.(project.project_id))
      .catch(() => undefined)
      .finally(() => setPathSaving(false));
  }

  function revokeSkillTrust() {
    if (!onRevokeSkillTrust || revoking) return;
    setRevoking(true);
    void Promise.resolve(onRevokeSkillTrust(project.project_id))
      .catch(() => undefined)
      .finally(() => setRevoking(false));
  }

  const content = (
    <List
      size="small"
      split={false}
      dataSource={[
        { key: "rename", label: "修改项目名称", onClick: () => { setOpen(false); setRenameError(""); setRenameOpen(true); } },
        { key: "path", label: "修改项目路径", onClick: openPathPicker, disabled: pathSaving },
        {
          key: "skill-trust",
          label: "撤销项目 Skill 信任",
          onClick: () => { setOpen(false); revokeSkillTrust(); },
          disabled: revoking,
          loading: revoking,
        },
        { key: "remove", label: "删除项目", danger: true, onClick: () => { setOpen(false); onConfirmRemove(project); } },
      ]}
      renderItem={(item) => (
        <List.Item style={{ padding: 0 }}>
          <Button
            type="text"
            block
            danger={item.danger}
            disabled={item.disabled}
            loading={item.key === "path" && pathSaving || item.key === "skill-trust" && revoking}
            onClick={item.onClick}
            style={{ textAlign: "left" }}
          >
            {item.label}
          </Button>
        </List.Item>
      )}
    />
  );

  return (
    <>
      <Popover title="项目设置" content={content} trigger="click" open={open} onOpenChange={setOpen} placement="bottomRight">
        <Button
          type="text"
          size="small"
          icon={<SettingOutlined />}
          aria-label={`项目设置 ${project.name}`}
          onClick={(event) => event.stopPropagation()}
        />
      </Popover>
      <Modal
        title={`修改项目名称：${project.name}`}
        open={renameOpen}
        onCancel={() => { if (!renameSaving) setRenameOpen(false); }}
        okText="保存"
        cancelText="取消"
        confirmLoading={renameSaving}
        onOk={() => void saveName()}
        destroyOnHidden
      >
        <Input
          aria-label="项目名称"
          autoFocus
          maxLength={120}
          value={draftName}
          onChange={(event) => setDraftName(event.target.value)}
          status={renameError ? "error" : undefined}
        />
        {renameError ? <Typography.Text type="danger">{renameError}</Typography.Text> : null}
      </Modal>
    </>
  );
}

function HistoryRow({ conversation, selected, onSelect, onRename, onArchive, onDelete }: HistoryRowProps) {
  const title = conversation.title || "新对话";
  const running = conversation.messages.some((message) => message.running);
  const messageCount =
    conversation.messages.length > 0
      ? conversation.messages.filter((message) => message.role === "user" || message.role === "assistant").length
      : conversation.messageCount ?? 0;
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
  projects,
  projectsLoaded = true,
  projectLoading,
  onNewProject,
  onNewProjectConversation,
  onRemoveProject,
  onRenameProject,
  onChangeProjectPath,
  onRevokeSkillTrust,
  onSelect,
  onNavigate,
  onRename,
  onArchive,
  onDelete,
  onSignOut,
  onProfileUpdate,
  onOpenSettings,
}: AppSidebarProps) {
  const { modal } = AntApp.useApp();
  const projectStorageKey = `mini-agent-project-collapse:${user?.id ?? "anonymous"}`;
  const [expandedProjectIds, setExpandedProjectIds] = useState<string[]>([]);
  const loadedProjectStorageKey = useRef<string | null>(null);
  const previousCurrentProjectId = useRef<string | undefined>(undefined);
  const currentProjectId = conversations.find((conversation) => conversation.id === currentId)?.projectId;
  const displayName = user?.display_name?.trim() || (user?.kind === "guest" ? "游客用户" : "用户");

  useEffect(() => {
    if (loadedProjectStorageKey.current !== projectStorageKey) return;
    try {
      localStorage.setItem(projectStorageKey, JSON.stringify(expandedProjectIds));
    } catch {
      // Browser storage can be disabled; Collapse remains fully usable in memory.
    }
  }, [expandedProjectIds, projectStorageKey]);

  useEffect(() => {
    try {
      const parsed = JSON.parse(localStorage.getItem(projectStorageKey) ?? "[]");
      const storedIds =
        Array.isArray(parsed)
          ? parsed.filter((item): item is string => typeof item === "string")
          : [];
      if (currentProjectId && !storedIds.includes(currentProjectId)) storedIds.push(currentProjectId);
      setExpandedProjectIds(storedIds);
      loadedProjectStorageKey.current = projectStorageKey;
    } catch {
      setExpandedProjectIds([]);
      loadedProjectStorageKey.current = projectStorageKey;
    }
  }, [projectStorageKey]);

  useEffect(() => {
    if (!projectsLoaded) return;
    const knownProjectIds = new Set((projects ?? []).map((project) => project.project_id));
    setExpandedProjectIds((previous) => previous.filter((projectId) => knownProjectIds.has(projectId)));
  }, [projects, projectsLoaded]);

  useEffect(() => {
    if (previousCurrentProjectId.current === currentProjectId) return;
    previousCurrentProjectId.current = currentProjectId;
    if (!currentProjectId) return;
    setExpandedProjectIds((previous) => previous.includes(currentProjectId) ? previous : [...previous, currentProjectId]);
  }, [currentProjectId]);

  const confirmRemove = (project: ProjectInfo) => {
    const confirm = typeof modal?.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;
    confirm({
      title: `移除项目“${project.name}”？`,
      content: "只会从侧边栏隐藏项目及其历史，不会删除本地文件夹。可在回收站中恢复。",
      okText: "移除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => onRemoveProject?.(project.project_id),
    });
  };

  const projectItems = (projects ?? []).map((project) => ({
    key: project.project_id,
    label: (
      <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{project.name}</span>
        {!project.available ? <WarningOutlined aria-label="项目目录不可用" title="项目目录不可用" /> : null}
      </span>
    ),
    extra: (
      <span onClick={(event) => event.stopPropagation()}>
        <Button
          type="text"
          size="small"
          icon={<PlusOutlined />}
          disabled={!project.available}
          aria-label={`在项目 ${project.name} 中新建对话`}
          onClick={() => void onNewProjectConversation?.(project.project_id)}
        />
        <ProjectSettings
          project={project}
          onRenameProject={onRenameProject}
          onChangeProjectPath={onChangeProjectPath}
          onConfirmRemove={confirmRemove}
          onRevokeSkillTrust={onRevokeSkillTrust}
        />
      </span>
    ),
    children: conversations.filter((conversation) => conversation.projectId === project.project_id).map((conversation) => (
      <HistoryRow
        key={conversation.id}
        conversation={conversation}
        selected={conversation.id === currentId && page === "chat"}
        onSelect={onSelect}
        onRename={onRename}
        onArchive={onArchive}
        onDelete={onDelete}
      />
    )),
  }));

  const projectHistory = (
    <>
      <Typography.Text type="secondary" style={{ margin: "20px 8px 8px", fontSize: 12 }}>
        项目对话
      </Typography.Text>
      <div className="project-history-list" style={{ minHeight: 0, maxHeight: 360, overflowY: "auto" }}>
        <Collapse
          ghost
          activeKey={expandedProjectIds}
          onChange={(keys) => setExpandedProjectIds(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
          items={projectItems}
        />
      </div>
    </>
  );

  const ordinaryHistory = (
    <>
      <Typography.Text type="secondary" style={{ margin: "12px 8px 8px", fontSize: 12 }}>
        无项目对话
      </Typography.Text>
      <div style={{ minHeight: 0, flex: 1, overflowY: "auto" }}>
        <List
          size="small"
          split={false}
          dataSource={conversations.filter((conversation) => !conversation.projectId)}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" /> }}
          renderItem={(conversation) => (
            <HistoryRow
              key={conversation.id}
              conversation={conversation}
              selected={conversation.id === currentId && page === "chat"}
              onSelect={onSelect}
              onRename={onRename}
              onArchive={onArchive}
              onDelete={onDelete}
            />
          )}
        />
      </div>
    </>
  );
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
        type="default"
        className="sidebar-create-button"
        size="large"
        block
        icon={<PlusOutlined />}
        onClick={() => void onNew()}
        aria-label="新建对话"
      >
        新建对话
      </Button>

      <Button
        type="default"
        className="sidebar-create-button"
        block
        icon={<FolderOpenOutlined />}
        loading={projectLoading}
        onClick={() => void onNewProject?.()}
        aria-label="新建项目"
        style={{ marginTop: 8, textAlign: "left" }}
      >
        新建项目
      </Button>

      {projectHistory}
      {ordinaryHistory}

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
        <div className="sidebar-profile-row">
          {onOpenSettings ? (
            <Button
              className="profile-trigger"
              type="text"
              icon={<UserOutlined />}
              onClick={onOpenSettings}
              aria-label={"个人简介：" + displayName}
            >
              <ProfileLabel label={displayName} />
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
        </div>
      </div>
    </div>
  );
}

export { confirmDelete };
