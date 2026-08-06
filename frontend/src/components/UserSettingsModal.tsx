import {
  App as AntApp,
  Button,
  Form,
  Input,
  Menu,
  Modal,
  Select,
  Space,
  Spin,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  getSettings,
  updateAgentConfig,
  updateProfile,
  updateProviderConfig,
  type AgentConfig,
  type ProviderConfig,
  type UserSettings,
} from "../api";
import type { AuthUser } from "../types";

type SettingsSection = "profile" | "agent" | "provider";

interface UserSettingsModalProps {
  open: boolean;
  user: AuthUser | null;
  onClose: () => void;
  onUserUpdate: (user: Partial<AuthUser>) => void;
}

const defaultAgent: AgentConfig = {
  tone: "balanced",
  verbosity: "balanced",
  initiative: "balanced",
  custom_instructions: "",
};

const defaultProvider: ProviderConfig = {
  provider: "deepseek",
  protocol: "chat_completions",
  base_url: "",
  model: "",
  max_tokens: 8192,
  context_size: 1024000,
  tokenizer_model: "deepseek-ai/DeepSeek-V3",
  api_key_configured: false,
};

function snapshot(settings: UserSettings | null): string {
  return JSON.stringify(settings ?? null);
}

export default function UserSettingsModal({ open, user, onClose, onUserUpdate }: UserSettingsModalProps) {
  const { modal } = AntApp.useApp();
  const [section, setSection] = useState<SettingsSection>("profile");
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [saved, setSaved] = useState<UserSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setError("");
    void getSettings()
      .then((next) => {
        if (!active) return;
        setSettings(next);
        setSaved(next);
      })
      .catch((cause) => {
        if (!active) return;
        setSettings({
          profile: {
            email: user?.email ?? "",
            display_name: user?.display_name ?? "",
            agent_preferences: user?.agent_preferences ?? "",
          },
          agent_config: defaultAgent,
          provider_config: defaultProvider,
          capability_config: {},
        });
        setSaved(null);
        setError(cause instanceof Error ? cause.message : "设置加载失败。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, user?.display_name, user?.agent_preferences, user?.email]);

  const dirty = useMemo(() => snapshot(settings) !== snapshot(saved), [saved, settings]);

  function requestClose() {
    if (!dirty) {
      onClose();
      return;
    }
    modal.confirm({
      title: "放弃未保存的修改？",
      content: "当前修改尚未保存，关闭后将丢失。",
      okText: "放弃修改",
      cancelText: "继续编辑",
      onOk: onClose,
    });
  }

  function updateSettings(patch: Partial<UserSettings>) {
    setSettings((current) => (current ? { ...current, ...patch } : current));
  }

  async function saveCurrent() {
    if (!settings) return;
    setSaving(true);
    setError("");
    try {
      if (section === "profile") {
        const profile = await updateProfile({
          display_name: settings.profile.display_name.trim(),
          agent_preferences: settings.profile.agent_preferences.trim(),
        });
        updateSettings({ profile: { ...settings.profile, ...profile } });
        setSaved((current) => (current ? { ...current, profile: { ...current.profile, ...profile } } : current));
        onUserUpdate(profile);
      } else if (section === "agent") {
        const agent = await updateAgentConfig(settings.agent_config);
        updateSettings({ agent_config: agent });
        setSaved((current) => (current ? { ...current, agent_config: agent } : current));
      } else {
        const provider = await updateProviderConfig(settings.provider_config);
        updateSettings({ provider_config: provider });
        setSaved((current) => (current ? { ...current, provider_config: provider } : current));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  }

  const menuItems = [
    { key: "profile", label: "个人简介" },
    { key: "agent", label: "Agent 配置" },
    { key: "provider", label: "提供商" },
  ];

  const body = loading || !settings ? (
    <div className="user-settings-loading"><Spin /></div>
  ) : (
    <div className="user-settings-layout">
      <nav className="user-settings-nav" aria-label="设置目录">
        <Menu
          mode="inline"
          selectedKeys={[section]}
          items={menuItems}
          onClick={({ key }) => setSection(key as SettingsSection)}
        />
      </nav>
      <section className="user-settings-detail">
        {section === "profile" && (
          <Form layout="vertical">
            <Typography.Title level={4}>个人简介</Typography.Title>
            <Form.Item label="邮箱">
              <Input value={settings.profile.email} disabled />
            </Form.Item>
            <Form.Item label="显示名称">
              <Input
                aria-label="显示名称"
                maxLength={80}
                value={settings.profile.display_name}
                onChange={(event) => updateSettings({
                  profile: { ...settings.profile, display_name: event.target.value },
                })}
              />
            </Form.Item>
            <Form.Item label="兼容偏好文本">
              <Input.TextArea
                aria-label="兼容偏好文本"
                maxLength={4000}
                autoSize={{ minRows: 3, maxRows: 8 }}
                value={settings.profile.agent_preferences}
                onChange={(event) => updateSettings({
                  profile: { ...settings.profile, agent_preferences: event.target.value },
                })}
              />
            </Form.Item>
          </Form>
        )}
        {section === "agent" && (
          <Form layout="vertical">
            <Typography.Title level={4}>Agent 配置</Typography.Title>
            <Form.Item label="语气">
              <Select
                value={settings.agent_config.tone}
                options={[
                  { value: "balanced", label: "平衡" },
                  { value: "formal", label: "正式" },
                  { value: "friendly", label: "友好" },
                  { value: "direct", label: "直接" },
                ]}
                onChange={(tone) => updateSettings({ agent_config: { ...settings.agent_config, tone } })}
              />
            </Form.Item>
            <Form.Item label="详略">
              <Select
                value={settings.agent_config.verbosity}
                options={[
                  { value: "balanced", label: "平衡" },
                  { value: "concise", label: "简洁" },
                  { value: "detailed", label: "详细" },
                ]}
                onChange={(verbosity) => updateSettings({ agent_config: { ...settings.agent_config, verbosity } })}
              />
            </Form.Item>
            <Form.Item label="主动性">
              <Select
                value={settings.agent_config.initiative}
                options={[
                  { value: "balanced", label: "平衡" },
                  { value: "reserved", label: "克制" },
                  { value: "proactive", label: "主动" },
                ]}
                onChange={(initiative) => updateSettings({ agent_config: { ...settings.agent_config, initiative } })}
              />
            </Form.Item>
            <Form.Item label="自由文本偏好">
              <Input.TextArea
                aria-label="自由文本偏好"
                maxLength={4000}
                autoSize={{ minRows: 7, maxRows: 14 }}
                value={settings.agent_config.custom_instructions}
                onChange={(event) => updateSettings({
                  agent_config: { ...settings.agent_config, custom_instructions: event.target.value },
                })}
              />
            </Form.Item>
          </Form>
        )}
        {section === "provider" && (
          <Form layout="vertical">
            <Typography.Title level={4}>提供商</Typography.Title>
            <Space.Compact block>
              <Form.Item label="协议" style={{ flex: 1 }}>
                <Select
                  value={settings.provider_config.protocol}
                  options={[
                    { value: "chat_completions", label: "Chat Completions" },
                    { value: "responses", label: "Responses" },
                    { value: "messages", label: "Messages" },
                  ]}
                  onChange={(protocol) => updateSettings({
                    provider_config: { ...settings.provider_config, protocol },
                  })}
                />
              </Form.Item>
              <Form.Item label="提供商" style={{ flex: 1 }}>
                <Input
                  value={settings.provider_config.provider}
                  onChange={(event) => updateSettings({
                    provider_config: { ...settings.provider_config, provider: event.target.value },
                  })}
                />
              </Form.Item>
            </Space.Compact>
            <Form.Item label="Base URL">
              <Input
                value={settings.provider_config.base_url}
                onChange={(event) => updateSettings({
                  provider_config: { ...settings.provider_config, base_url: event.target.value },
                })}
              />
            </Form.Item>
            <Form.Item label="模型">
              <Input
                value={settings.provider_config.model}
                onChange={(event) => updateSettings({
                  provider_config: { ...settings.provider_config, model: event.target.value },
                })}
              />
            </Form.Item>
            <Space.Compact block>
              <Form.Item label="最大输出 token" style={{ flex: 1 }}>
                <Input
                  type="number"
                  value={settings.provider_config.max_tokens}
                  onChange={(event) => updateSettings({
                    provider_config: { ...settings.provider_config, max_tokens: Number(event.target.value) },
                  })}
                />
              </Form.Item>
              <Form.Item label="上下文大小" style={{ flex: 1 }}>
                <Input
                  type="number"
                  value={settings.provider_config.context_size}
                  onChange={(event) => updateSettings({
                    provider_config: { ...settings.provider_config, context_size: Number(event.target.value) },
                  })}
                />
              </Form.Item>
            </Space.Compact>
            <Form.Item label="API Key">
              <Input.Password
                placeholder={settings.provider_config.api_key_configured ? "已配置，留空以保持不变" : "输入 API Key"}
                onChange={(event) => updateSettings({
                  provider_config: { ...settings.provider_config, api_key: event.target.value } as ProviderConfig,
                })}
              />
            </Form.Item>
          </Form>
        )}
        {error ? <Typography.Text type="danger">{error}</Typography.Text> : null}
      </section>
    </div>
  );

  return (
    <Modal
      className="user-settings-modal"
      title="用户设置"
      open={open}
      width={900}
      centered
      maskClosable={false}
      keyboard={false}
      onCancel={requestClose}
      footer={(
        <Space>
          <Button
            type="primary"
            aria-label="保存"
            loading={saving}
            disabled={loading || !settings}
            onClick={() => void saveCurrent()}
          >
            保存
          </Button>
        </Space>
      )}
    >
      {body}
    </Modal>
  );
}