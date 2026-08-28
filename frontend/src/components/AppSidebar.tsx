import {
  BarChartOutlined,
  DeleteOutlined,
  FolderOpenOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  UserOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { App as AntApp, Badge, Button, Collapse, Divider, Empty, List, Modal, Typography } from "antd";
import { useMemo } from "react";
import type { ProjectInfo } from "../api";
import { HistoryRow, confirmDelete } from "./sidebar/ConversationHistory";
import { ProfileLabel, ProfilePopover } from "./sidebar/ProfileSection";
import { ProjectSettings } from "./sidebar/ProjectSettings";
import type { AppSidebarProps } from "./sidebar/types";
import { useProjectExpansion } from "./sidebar/useProjectExpansion";

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
          <Typography.Text type="secondary" style={{ margin: "12px 8px 8px", fontSize: 12 }}>无项目对话</Typography.Text>
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
