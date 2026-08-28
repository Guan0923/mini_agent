import { SettingOutlined } from "@ant-design/icons";
import { Button, Input, List, Modal, Popover, Typography } from "antd";
import { useEffect, useState } from "react";
import type { ProjectInfo } from "../../api";

interface ProjectSettingsProps {
  project: ProjectInfo;
  onRenameProject?: (projectId: string, name: string) => void | Promise<unknown>;
  onChangeProjectPath?: (projectId: string) => void | Promise<unknown>;
  onConfirmRemove: (project: ProjectInfo) => void;
  onRevokeSkillTrust?: (projectId: string) => void | Promise<unknown>;
}

export function ProjectSettings({
  project,
  onRenameProject,
  onChangeProjectPath,
  onConfirmRemove,
  onRevokeSkillTrust,
}: ProjectSettingsProps) {
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
            loading={(item.key === "path" && pathSaving) || (item.key === "skill-trust" && revoking)}
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
      <Popover
        title="项目设置"
        content={content}
        trigger="click"
        open={open}
        onOpenChange={setOpen}
        placement="bottomRight"
      >
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
