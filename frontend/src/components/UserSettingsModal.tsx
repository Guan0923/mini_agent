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
  Switch,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  getSettings,
  setTimezone,
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
  activeSessionId?: string;
  onAgentConfigUpdate?: (config: AgentConfig) => void;
  onUserUpdate: (user: Partial<AuthUser>) => void;
}

const defaultAgent: AgentConfig = {
  tone: "balanced",
  verbosity: "balanced",
  initiative: "balanced",
  display_mode: "medium",
  timezone: "Asia/Shanghai",
  location_enabled: false,
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

export default function UserSettingsModal({ open, user, onClose, onUserUpdate, activeSessionId, onAgentConfigUpdate }: UserSettingsModalProps) {
  const { modal } = AntApp.useApp();
  const [section, setSection] = useState<SettingsSection>("profile");
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [saved, setSaved] = useState<UserSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [locationError, setLocationError] = useState("");

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
          timezone_options: [],
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
  async function toggleLocation(enabled: boolean): Promise<void> {
    if (!settings) return;
    setLocationError("");
    if (!enabled) {
      updateSettings({ agent_config: { ...settings.agent_config, location_enabled: false } });
      return;
    }
    if (!navigator.geolocation) {
      updateSettings({ agent_config: { ...settings.agent_config, location_enabled: false } });
      setLocationError("当前浏览器不支持定位，请手动选择时区。");
      return;
    }

    await new Promise<void>((resolve) => {
      navigator.geolocation.getCurrentPosition(
        () => {
          const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
          const supported = (settings.timezone_options ?? []).some((option) => option.identifier === timezone);
          if (!timezone || !supported) {
            updateSettings({ agent_config: { ...settings.agent_config, location_enabled: false } });
            setLocationError("浏览器时区暂不受支持，请手动选择时区。");
          } else {
            updateSettings({ agent_config: { ...settings.agent_config, timezone, location_enabled: true } });
          }
          resolve();
        },
        (cause) => {
          updateSettings({ agent_config: { ...settings.agent_config, location_enabled: false } });
          setLocationError(cause.code === 1 ? "定位权限被拒绝，请手动选择时区。" : "无法获取定位，请手动选择时区。");
          resolve();
        },
        { maximumAge: 0, timeout: 10000 },
      );
    });
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
        if (activeSessionId && saved?.agent_config.timezone !== settings.agent_config.timezone) {
          await setTimezone(activeSessionId, settings.agent_config.timezone);
        }
        updateSettings({ agent_config: agent });
        setSaved((current) => (current ? { ...current, agent_config: agent } : current));
        onAgentConfigUpdate?.(agent);
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
            <Form.Item label="回答风格">
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
            <Form.Item label="运行信息详略">
              <Select
                aria-label="运行信息详略"
                value={settings.agent_config.display_mode}
                options={[
                  { value: "minimal", label: "最少" },
                  { value: "medium", label: "标准" },
                  { value: "verbose", label: "详细" },
                ]}
                onChange={(display_mode) => updateSettings({ agent_config: { ...settings.agent_config, display_mode } })}
              />
            </Form.Item>
            <Form.Item label="时区">
              <Select
                aria-label="Agent 默认时区"
                showSearch
                optionFilterProp="label"
                value={settings.agent_config.timezone}
                options={(settings.timezone_options ?? []).map((option) => ({
                  value: option.identifier,
                  label: `${option.label} (${option.identifier})`,
                }))}
                onChange={(timezone) => {
                  setLocationError("");
                  updateSettings({ agent_config: { ...settings.agent_config, timezone, location_enabled: false } });
                }}
                notFoundContent="暂无可用时区"
              />
            </Form.Item>
            <Form.Item label="允许获取定位以自动设置时区">
              <Switch
                checked={settings.agent_config.location_enabled}
                onChange={(checked) => void toggleLocation(checked)}
              />
            </Form.Item>
            {locationError ? <Typography.Text type="danger">{locationError}</Typography.Text> : null}
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