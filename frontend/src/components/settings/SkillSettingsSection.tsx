import { DeleteOutlined, ImportOutlined } from "@ant-design/icons";
import { Alert, App, Button, Descriptions, List, Space, Spin, Switch, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import {
  deleteSkill,
  getSkillSettings,
  importSkill,
  setSkillEnabled,
  setSkillsEnabled,
  type SkillSettingsResponse,
} from "../../api";

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error ? cause.message : fallback;
}

export function SkillSettingsSection() {
  const { modal, message } = App.useApp();
  const [data, setData] = useState<SkillSettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [globalSaving, setGlobalSaving] = useState(false);
  const [rowSaving, setRowSaving] = useState<Record<string, boolean>>({});
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      setData(await getSkillSettings());
    } catch (cause) {
      setError(errorMessage(cause, "Skill 设置加载失败。"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function toggleGlobal(enabled: boolean) {
    if (!data) return;
    const previousEnabled = data.enabled;
    setData((current) => current ? { ...current, enabled } : current);
    setGlobalSaving(true);
    setError("");
    try {
      setData(await setSkillsEnabled(enabled));
    } catch (cause) {
      setData((current) => current ? { ...current, enabled: previousEnabled } : current);
      setError(errorMessage(cause, "Skill 总开关保存失败。"));
    } finally {
      setGlobalSaving(false);
    }
  }

  async function toggleSkill(directory: string, enabled: boolean) {
    if (!data) return;
    const previousEnabled = data.skills.find((item) => item.directory === directory)?.enabled ?? !enabled;
    setData((current) => current ? {
      ...current,
      skills: current.skills.map((item) => item.directory === directory ? { ...item, enabled } : item),
    } : current);
    setRowSaving((current) => ({ ...current, [directory]: true }));
    setError("");
    try {
      setData(await setSkillEnabled(directory, enabled));
    } catch (cause) {
      setData((current) => current ? {
        ...current,
        skills: current.skills.map((item) => (
          item.directory === directory ? { ...item, enabled: previousEnabled } : item
        )),
      } : current);
      setError(errorMessage(cause, `Skill ${directory} 开关保存失败。`));
    } finally {
      setRowSaving((current) => ({ ...current, [directory]: false }));
    }
  }

  async function chooseAndImport() {
    setImporting(true);
    setError("");
    try {
      const result = await importSkill();
      if (result) {
        message.success(`已导入 Skill：${result.directory}`);
        await load();
      }
    } catch (cause) {
      setError(errorMessage(cause, "Skill 导入失败。"));
    } finally {
      setImporting(false);
    }
  }

  function confirmDelete(directory: string, name: string) {
    modal.confirm({
      title: `永久删除 Skill “${name}”？`,
      content: `将删除用户 Skill 目录 ${directory} 及其中全部文件，此操作无法恢复。`,
      okText: "永久删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setRowSaving((current) => ({ ...current, [directory]: true }));
        try {
          await deleteSkill(directory);
          message.success(`已删除 Skill：${name}`);
          await load();
        } catch (cause) {
          setError(errorMessage(cause, `Skill ${directory} 删除失败。`));
          throw cause;
        } finally {
          setRowSaving((current) => ({ ...current, [directory]: false }));
        }
      },
    });
  }

  if (loading && !data) {
    return <div className="user-settings-loading"><Spin /></div>;
  }

  return (
    <div>
      <Typography.Title level={4}>Skill</Typography.Title>
      <Typography.Paragraph type="secondary">
        开关与导入、删除会立即保存，仅影响下一个 Turn；正在运行的 Turn 不会热更新。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }} wrap>
        <Switch
          aria-label="启用 Skill"
          checked={data?.enabled ?? true}
          loading={globalSaving}
          onChange={(enabled) => void toggleGlobal(enabled)}
        />
        <Typography.Text>启用用户 Skill</Typography.Text>
        <Button icon={<ImportOutlined />} loading={importing} onClick={() => void chooseAndImport()}>
          导入 Skill
        </Button>
      </Space>
      {error ? <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} /> : null}
      <List
        loading={loading}
        dataSource={data?.skills ?? []}
        locale={{ emptyText: "暂无可发现的用户 Skill" }}
        renderItem={(skill) => (
          <List.Item
            key={skill.directory}
            actions={[
              <Switch
                key="enabled"
                aria-label={`启用 Skill ${skill.name}`}
                checked={skill.enabled}
                loading={rowSaving[skill.directory]}
                disabled={!data?.enabled}
                onChange={(enabled) => void toggleSkill(skill.directory, enabled)}
              />,
              <Button
                key="delete"
                type="text"
                danger
                aria-label={`删除 Skill ${skill.name}`}
                icon={<DeleteOutlined />}
                loading={rowSaving[skill.directory]}
                onClick={() => confirmDelete(skill.directory, skill.name)}
              />,
            ]}
          >
            <List.Item.Meta
              title={<Space wrap><span>{skill.name}</span><Tag>{skill.directory}</Tag></Space>}
              description={(
                <Space orientation="vertical" size={8} style={{ width: "100%" }}>
                  <Typography.Text type="secondary">{skill.description}</Typography.Text>
                  <Descriptions
                    size="small"
                    column={1}
                    items={[
                      { key: "root", label: "来源", children: skill.root },
                      {
                        key: "metadata",
                        label: "Metadata",
                        children: Object.keys(skill.metadata).length
                          ? Object.entries(skill.metadata).map(([key, value]) => <Tag key={key}>{key}: {value}</Tag>)
                          : "无",
                      },
                      {
                        key: "tools",
                        label: "Allowed tools",
                        children: skill.allowed_tools.length
                          ? skill.allowed_tools.map((tool) => <Tag key={tool}>{tool}</Tag>)
                          : "未声明",
                      },
                    ]}
                  />
                </Space>
              )}
            />
          </List.Item>
        )}
      />
    </div>
  );
}
