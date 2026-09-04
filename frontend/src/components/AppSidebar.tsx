import {
  BarChartOutlined,
  DeleteOutlined,
  FolderOpenOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  SortAscendingOutlined,
  UserOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { App as AntApp, Badge, Button, Collapse, Divider, Dropdown, Modal, Typography, type MenuProps } from "antd";
import { useMemo, useRef, useState } from "react";
import type { ProjectInfo, SidebarThreadSort } from "../api";
import { confirmDelete } from "./sidebar/ConversationHistory";
import { ProfileLabel, ProfilePopover } from "./sidebar/ProfileSection";
import { ProjectSettings } from "./sidebar/ProjectSettings";
import { SortableHistoryList } from "./sidebar/SortableHistoryList";
import type { AppSidebarProps } from "./sidebar/types";
import { useProjectExpansion } from "./sidebar/useProjectExpansion";

interface ConversationSortButtonProps {
  count: number;
  loading: boolean;
  onSort: (sortBy: SidebarThreadSort) => void;
}

function ConversationSortButton({ count, loading, onSort }: ConversationSortButtonProps) {
  const menu: MenuProps = {
    items: [
      { key: "created_at", label: "按创建时间" },
      { key: "recent_activity", label: "按最近聊天" },
    ],
    onClick: ({ key }) => onSort(key as SidebarThreadSort),
  };
  return (
    <Dropdown menu={menu} trigger={["click"]} placement="bottomRight" disabled={loading || count < 2}>
      <Button
        type="text"
        size="small"
        loading={loading}
        icon={<SortAscendingOutlined />}
        aria-label="对话排序"
        title="对话排序"
      />
    </Dropdown>
  );
}

export default function AppSidebar({
  profile,
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
  onReorder,
  onSort,
  onProfileUpdate,
  onOpenSettings,
  collapsed = false,
  onToggleCollapse,
  revealKey = 0,
}: AppSidebarProps) {
  const { modal } = AntApp.useApp();
  const currentProjectId = conversations.find((conversation) => conversation.id === currentId)?.projectId;
  const projectIds = useMemo(() => (projects ?? []).map((project) => project.project_id), [projects]);
  const { expandedProjectIds, setExpandedProjectIds } = useProjectExpansion(projectIds, currentProjectId, projectsLoaded);
  const displayName = profile.display_name.trim() || "本地用户";
  const [savingScopes, setSavingScopes] = useState<Set<string>>(() => new Set());
  const groupActionQueues = useRef(new Map<string, Promise<void>>());

  function scopeKey(projectId: string | null): string {
    return projectId ?? "__unassigned__";
  }

  async function runGroupAction(projectId: string | null, action: () => Promise<void>) {
    const key = scopeKey(projectId);
    const previous = groupActionQueues.current.get(key) ?? Promise.resolve();
    const queued = previous.catch(() => undefined).then(action);
    groupActionQueues.current.set(key, queued);
    setSavingScopes((current) => new Set(current).add(key));
    try {
      await queued;
    } finally {
      if (groupActionQueues.current.get(key) === queued) {
        groupActionQueues.current.delete(key);
        setSavingScopes((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        });
      }
    }
  }

  function confirmRemove(project: ProjectInfo) {
    const confirm = typeof modal?.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;
    confirm({
      title: `移除项目“${project.name}”？`,
      content: "只会从侧边栏隐藏项目及其历史，不会删除本地文件夹。可在回收站中恢复。",
      okText: "移除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => onRemoveProject?.(project.project_id),
    });
  }

  const projectItems = (projects ?? []).map((project) => {
    const group = conversations.filter((conversation) => conversation.projectId === project.project_id);
    const saving = savingScopes.has(scopeKey(project.project_id));
    return {
      key: project.project_id,
      label: (
        <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{project.name}</span>
          {!project.available ? <WarningOutlined aria-label="项目目录不可用" title="项目目录不可用" /> : null}
        </span>
      ),
      extra: (
        <span onClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()}>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            disabled={!project.available}
            aria-label={`在项目 ${project.name} 中新建对话`}
            onClick={() => void onNewProjectConversation?.(project.project_id)}
          />
          <ConversationSortButton
            count={group.length}
            loading={saving}
            onSort={(sortBy) => void runGroupAction(project.project_id, () => onSort?.(project.project_id, sortBy) ?? Promise.resolve())}
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
      children: (
        <SortableHistoryList
          conversations={group}
          currentId={currentId}
          pageIsChat={page === "chat"}
          disabled={saving || !onReorder}
          onSelect={onSelect}
          onRename={onRename}
          onArchive={onArchive}
          onDelete={onDelete}
          onReorder={(orderedThreadIds) => runGroupAction(project.project_id, () => onReorder?.(project.project_id, orderedThreadIds) ?? Promise.resolve())}
        />
      ),
    };
  });

  const ordinaryConversations = conversations.filter((conversation) => !conversation.projectId);
  const ordinarySaving = savingScopes.has(scopeKey(null));

  return (
    <div
      className="app-sidebar"
      style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, padding: 16, background: "#f4f7f8" }}
    >
      <div className={`sidebar-reveal-shell${page === "chat" ? " sidebar-reveal-active" : ""}`} key={revealKey}>
        <div className="sidebar-header sidebar-reveal-item" data-reveal-index="0">
          <Typography.Text className="sidebar-project-title">Mini-Agent</Typography.Text>
          {onToggleCollapse ? (
            <Button
              className="sidebar-collapse-button"
              type="text"
              size="small"
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={onToggleCollapse}
              aria-label={collapsed ? "展开侧边栏" : "折叠侧边栏"}
              aria-expanded={!collapsed}
              aria-controls="chat-sidebar"
            />
          ) : null}
        </div>

        <div className="sidebar-primary-actions sidebar-reveal-item" data-reveal-index="1">
          <Button type="default" className="sidebar-create-button" block icon={<PlusOutlined />} onClick={() => void onNew()} aria-label="新建对话">
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
        </div>

        <div className="sidebar-project-history sidebar-reveal-item" data-reveal-index="2">
          <Typography.Text type="secondary" style={{ margin: "20px 8px 8px", fontSize: 12 }}>项目对话</Typography.Text>
          <div className="project-history-list" style={{ minHeight: 0, maxHeight: 360, overflowY: "auto" }}>
            <Collapse
              ghost
              activeKey={expandedProjectIds}
              onChange={(keys) => setExpandedProjectIds(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
              items={projectItems}
            />
          </div>
        </div>

        <div className="sidebar-ordinary-history sidebar-reveal-item" data-reveal-index="3">
          <div className="sidebar-section-heading">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>无项目对话</Typography.Text>
            <ConversationSortButton
              count={ordinaryConversations.length}
              loading={ordinarySaving}
              onSort={(sortBy) => void runGroupAction(null, () => onSort?.(null, sortBy) ?? Promise.resolve())}
            />
          </div>
          <div style={{ minHeight: 0, flex: 1, overflowY: "auto" }}>
            <SortableHistoryList
              conversations={ordinaryConversations}
              currentId={currentId}
              pageIsChat={page === "chat"}
              disabled={ordinarySaving || !onReorder}
              onSelect={onSelect}
              onRename={onRename}
              onArchive={onArchive}
              onDelete={onDelete}
              onReorder={(orderedThreadIds) => runGroupAction(null, () => onReorder?.(null, orderedThreadIds) ?? Promise.resolve())}
            />
          </div>
        </div>

        <div className="sidebar-utility-section sidebar-reveal-item" data-reveal-index="4">
          <Divider style={{ margin: "12px 0" }} />
          <div className="sidebar-utility-links">
            <Badge className="sidebar-utility-badge" count={archivedCount} size="small" offset={[5, 0]}>
              <Button
                type={page === "trash" ? "default" : "text"}
                className="sidebar-create-button"
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
              className="sidebar-create-button"
              block
              icon={<BarChartOutlined />}
              onClick={() => onNavigate("benchmark")}
              aria-label="Benchmark"
              style={{ textAlign: "left" }}
            >
              Benchmark
            </Button>
          </div>
        </div>

        <div className="sidebar-footer sidebar-reveal-item" data-reveal-index="5">
          <div className="sidebar-profile-row">
            {onOpenSettings ? (
              <Button className="profile-trigger" type="text" icon={<UserOutlined />} onClick={onOpenSettings} aria-label={`个人简介：${displayName}`}>
                <ProfileLabel label={displayName} />
              </Button>
            ) : (
              <ProfilePopover profile={profile} onSave={onProfileUpdate} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export { confirmDelete };
