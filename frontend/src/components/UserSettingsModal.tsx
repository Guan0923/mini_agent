import {
  App as AntApp,
  Alert,
  Button,
  AutoComplete,
  Collapse,
  Form,
  Input,
  InputNumber,
  List,
  Menu,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { useEffect, useMemo, useState } from "react";
import {
  getSettings,
  addProviderConfig,
  activateProviderConfig,
  deleteProviderConfig,
  discoverProviderModels,
  getCloudSnapshots,
  getSyncJob,
  getSyncStatus,
  restoreCloudSnapshot,
  saveToCloud,
  setTimezone,
  updateAgentConfig,
  updateProfile,
  updateRuntimeConfig,
  updateProviderConfigById,
  updateSyncPreferences,
  type AgentConfig,
  type ProviderConfig,
  type UserSettings,
  type RuntimeConfig,
  type CloudSnapshot,
  type SyncJob,
} from "../api";
import type { AuthUser } from "../types";

type SettingsSection = "profile" | "agent" | "runtime" | "provider_add" | "provider_manage" | "cloud";

type ProviderDraft = {
  provider: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  model: string;
  max_tokens: number;
  context_size: number;
  tokenizer_model: string;
  api_key: string;
};

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
  id: "",
  is_active: false,
  provider: "deepseek",
  protocol: "chat_completions",
  base_url: "",
  model: "",
  max_tokens: 8192,
  context_size: 1024000,
  tokenizer_model: "deepseek-ai/DeepSeek-V3",
  api_key_configured: false,
};

const defaultProviderDraft: ProviderDraft = {
  provider: defaultProvider.provider,
  protocol: defaultProvider.protocol,
  base_url: defaultProvider.base_url,
  model: defaultProvider.model,
  max_tokens: defaultProvider.max_tokens,
  context_size: defaultProvider.context_size,
  tokenizer_model: defaultProvider.tokenizer_model,
  api_key: "",
};

const defaultSyncPreferences = {
  auto_save_enabled: false,
  auto_save_rule: "idle_5m" as const,
};

const defaultSyncState = {
  local_revision: 0,
  uploaded_revision: 0,
  cloud_snapshot_id: null,
  status: "local_only" as const,
  last_error: "",
  updated_at: null,
};

function snapshot(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
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
  const [providerAddDraft, setProviderAddDraft] = useState<ProviderDraft>(defaultProviderDraft);
  const [savedProviderAddDraft, setSavedProviderAddDraft] = useState<ProviderDraft>(defaultProviderDraft);
  const [providerDrafts, setProviderDrafts] = useState<Record<string, { model: string; api_key: string }>>({});
  const [savedProviderDrafts, setSavedProviderDrafts] = useState<Record<string, { model: string; api_key: string }>>({});
  const [modelOptions, setModelOptions] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState<Record<string, boolean>>({});
  const [syncJob, setSyncJob] = useState<SyncJob | null>(null);
  const [cloudSnapshots, setCloudSnapshots] = useState<CloudSnapshot[]>([]);
  const [cloudLoading, setCloudLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let mounted = true;
    setLoading(true);
    setError("");
    void getSettings()
      .then((next) => {
        if (!mounted) return;
        const providers = next.provider_configs ?? (next.provider_config?.id ? [next.provider_config] : []);
        const currentProvider = providers.find((provider) => provider.is_active)
          ?? (next.provider_config?.id ? next.provider_config : defaultProvider);
        const normalized = {
          ...next,
          provider_config: currentProvider,
          provider_configs: providers,
          sync_preferences: next.sync_preferences ?? defaultSyncPreferences,
          sync_state: next.sync_state ?? defaultSyncState,
          runtime_config: next.runtime_config ?? { max_tool_calls: 32 },
        };
        const drafts = Object.fromEntries(providers.map((provider) => [provider.id, { model: provider.model, api_key: "" }]));
        setSettings(normalized);
        setSaved(normalized);
        setProviderAddDraft(defaultProviderDraft);
        setSavedProviderAddDraft(defaultProviderDraft);
        setProviderDrafts(drafts);
        setSavedProviderDrafts(drafts);
        setModelOptions({});
        setModelsLoading({});
        if ((next.cloud_sync_available ?? user?.kind !== "guest") && user?.kind !== "guest") void refreshCloud();
      })
      .catch((cause) => {
        if (!mounted) return;
        const fallback: UserSettings = {
          profile: {
            email: user?.email ?? "",
            display_name: user?.display_name ?? "",
            agent_preferences: user?.agent_preferences ?? "",
          },
          agent_config: defaultAgent,
          provider_config: defaultProvider,
          provider_configs: [],
          capability_config: {},
          runtime_config: { max_tool_calls: 32 },
          timezone_options: [],
          sync_preferences: defaultSyncPreferences,
          sync_state: defaultSyncState,
        };
        setSettings(fallback);
        setSaved(fallback);
        setProviderAddDraft(defaultProviderDraft);
        setSavedProviderAddDraft(defaultProviderDraft);
        setProviderDrafts({});
        setSavedProviderDrafts({});
        setError(cause instanceof Error ? cause.message : "设置加载失败。");
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [open, user?.display_name, user?.agent_preferences, user?.email]);

  useEffect(() => {
    if (!open || !syncJob || !["queued", "running"].includes(syncJob.status)) return;
    const timer = window.setInterval(() => {
      void getSyncJob(syncJob.id)
        .then((job) => {
          setSyncJob(job);
          if (!["queued", "running"].includes(job.status)) void refreshCloud();
        })
        .catch((cause) => setError(cause instanceof Error ? cause.message : "读取云同步任务失败。"));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [open, syncJob?.id, syncJob?.status]);

  async function refreshCloud(): Promise<void> {
    if (user?.kind === "guest") return;
    try {
      const [status, snapshots] = await Promise.all([getSyncStatus(), getCloudSnapshots()]);
      setSyncJob(status.job);
      setCloudSnapshots(snapshots);
      setSettings((current) => current ? {
        ...current,
        sync_preferences: status.preferences,
        sync_state: status.state,
      } : current);
      setSaved((current) => current ? {
        ...current,
        sync_preferences: status.preferences,
        sync_state: status.state,
      } : current);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "云同步状态加载失败。");
    }
  }

  const dirty = useMemo(
    () => snapshot({ settings, providerAddDraft, providerDrafts }) !== snapshot({
      settings: saved,
      providerAddDraft: savedProviderAddDraft,
      providerDrafts: savedProviderDrafts,
    }),
    [saved, savedProviderAddDraft, settings, providerAddDraft, providerDrafts, savedProviderDrafts],
  );
  const cloudAvailable = user?.kind !== "guest" && settings?.cloud_sync_available !== false;

  function matchingModels(id: string, input: string): { value: string }[] {
    const query = input.trim().toLowerCase();
    return (modelOptions[id] ?? [])
      .filter((model) => !query || model.toLowerCase().includes(query))
      .map((model) => ({ value: model }));
  }

  function requestClose() {
    if (!dirty) {
      onClose();
      return;
    }
    modal.confirm({
      title: "退出用户设置？",
      content: "当前有未保存的修改，退出后将丢失。",
      okText: "退出",
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
      } else if (section === "runtime") {
        const runtime = await updateRuntimeConfig(settings.runtime_config);
        updateSettings({ runtime_config: runtime });
        setSaved((current) => (current ? { ...current, runtime_config: runtime } : current));
      } else if (section === "provider_add") {
        const provider = await addProviderConfig({
          provider: providerAddDraft.provider,
          protocol: providerAddDraft.protocol,
          base_url: providerAddDraft.base_url,
          model: providerAddDraft.model,
          max_tokens: providerAddDraft.max_tokens,
          context_size: providerAddDraft.context_size,
          tokenizer_model: providerAddDraft.tokenizer_model,
          api_key: providerAddDraft.api_key,
        });
        const providers = [...(settings.provider_configs ?? []), provider];
        updateSettings({ provider_configs: providers });
        setSaved((current) => (current ? { ...current, provider_configs: providers } : current));
        setProviderAddDraft(defaultProviderDraft);
        setSavedProviderAddDraft(defaultProviderDraft);
        setModelOptions((current) => {
          const next = { ...current };
          delete next.new;
          return next;
        });
      } else if (section === "cloud") {
        if (!cloudAvailable) return;
        const preferences = await updateSyncPreferences(settings.sync_preferences);
        updateSettings({ sync_preferences: preferences });
        setSaved((current) => current ? { ...current, sync_preferences: preferences } : current);
      } else {
        return;
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  }

  async function startCloudSave(force = false): Promise<void> {
    if (!cloudAvailable) return;
    setCloudLoading(true);
    setError("");
    try {
      const job = await saveToCloud(force);
      setSyncJob(job);
      await refreshCloud();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "启动云端保存失败。");
    } finally {
      setCloudLoading(false);
    }
  }

  function confirmRestore(snapshot: CloudSnapshot): void {
    modal.confirm({
      title: `恢复云端版本 ${snapshot.version}？`,
      content: "恢复会覆盖当前本地设置、会话和文件。系统会先保留一份本地恢复副本。",
      okText: "恢复",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        const job = await restoreCloudSnapshot(snapshot.id);
        setSyncJob(job);
      },
    });
  }

  function updateProviderDraft(id: string, patch: Partial<{ model: string; api_key: string }>) {
    setProviderDrafts((current) => ({ ...current, [id]: { ...(current[id] ?? { model: "", api_key: "" }), ...patch } }));
  }

  async function discoverModels(id: string, values: { provider: string; protocol: ProviderConfig["protocol"]; base_url: string; api_key?: string }) {
    setModelsLoading((current) => ({ ...current, [id]: true }));
    setError("");
    try {
      const result = await discoverProviderModels({ ...values, ...(id !== "new" ? { config_id: id } : {}) });
      setModelOptions((current) => ({ ...current, [id]: result.models }));
      if (result.models.length === 0) setError("模型服务没有返回可用模型，请继续手动输入。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "获取模型列表失败。");
    } finally {
      setModelsLoading((current) => ({ ...current, [id]: false }));
    }
  }

  async function saveManagedProvider(provider: ProviderConfig) {
    const draft = providerDrafts[provider.id] ?? { model: provider.model, api_key: "" };
    setSaving(true);
    setError("");
    try {
      const updated = await updateProviderConfigById(provider.id, {
        model: draft.model,
        ...(draft.api_key.trim() ? { api_key: draft.api_key } : {}),
      });
      const providers = (settings?.provider_configs ?? []).map((item) => item.id === updated.id ? updated : item);
      updateSettings({ provider_configs: providers, provider_config: updated.is_active ? updated : settings?.provider_config ?? defaultProvider });
      setSaved((current) => (current ? {
        ...current,
        provider_configs: providers,
        provider_config: updated.is_active ? updated : current.provider_config,
      } : current));
      const nextDraft = { model: updated.model, api_key: "" };
      setProviderDrafts((current) => ({ ...current, [updated.id]: nextDraft }));
      setSavedProviderDrafts((current) => ({ ...current, [updated.id]: nextDraft }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存提供商失败。");
    } finally {
      setSaving(false);
    }
  }

  function confirmDeleteProvider(provider: ProviderConfig) {
    modal.confirm({
      title: `删除提供商 ${provider.provider}？`,
      content: provider.is_active && (settings?.provider_configs.length ?? 0) > 1
        ? "当前提供商必须先切换后才能删除。"
        : "删除后将无法恢复该配置。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true, disabled: provider.is_active && (settings?.provider_configs.length ?? 0) > 1 },
      onOk: async () => {
        const providers = await deleteProviderConfig(provider.id);
        const active = providers.find((item) => item.is_active) ?? defaultProvider;
        updateSettings({ provider_configs: providers, provider_config: active });
        setSaved((current) => (current ? { ...current, provider_configs: providers, provider_config: active } : current));
        setProviderDrafts((current) => { const next = { ...current }; delete next[provider.id]; return next; });
        setSavedProviderDrafts((current) => { const next = { ...current }; delete next[provider.id]; return next; });
      },
    });
  }

  async function activateProvider(provider: ProviderConfig) {
    setSaving(true);
    setError("");
    try {
      const active = await activateProviderConfig(provider.id);
      const providers = (settings?.provider_configs ?? []).map((item) => ({ ...item, is_active: item.id === active.id }));
      updateSettings({ provider_configs: providers, provider_config: active });
      setSaved((current) => (current ? { ...current, provider_configs: providers, provider_config: active } : current));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "切换当前提供商失败。");
    } finally {
      setSaving(false);
    }
  }

  const menuItems = [
    { key: "profile", label: "个人简介" },
    { key: "agent", label: "Agent 配置" },
    { key: "runtime", label: "运行配置" },
    { key: "provider_add", label: "添加提供商" },
    { key: "provider_manage", label: "提供商管理" },
    { key: "cloud", label: "云同步" },
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
                value={!import.meta.env.DEV && settings.agent_config.display_mode === "developer" ? "verbose" : settings.agent_config.display_mode}
                options={[
                  { value: "minimal", label: "简洁" },
                  { value: "medium", label: "标准" },
                  { value: "verbose", label: "详细" },
                  ...(import.meta.env.DEV ? [{ value: "developer", label: "开发者" }] : []),
                ]}
                onChange={(display_mode) => updateSettings({ agent_config: { ...settings.agent_config, display_mode } })}
              />
            </Form.Item>
            <Form.Item label="时区">
              <Select
                aria-label="Agent 默认时区"
                showSearch={{ optionFilterProp: "label" }}
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
        {section === "runtime" && (
          <Form layout="vertical">
            <Typography.Title level={4}>运行配置</Typography.Title>
            <Form.Item label="工具调用上限">
              <InputNumber
                aria-label="工具调用上限"
                min={1}
                max={1000}
                step={1}
                precision={0}
                value={settings.runtime_config.max_tool_calls}
                onChange={(max_tool_calls) => {
                  if (typeof max_tool_calls === "number" && Number.isInteger(max_tool_calls)) {
                    updateSettings({ runtime_config: { max_tool_calls } as RuntimeConfig });
                  }
                }}
              />
              <Typography.Paragraph type="secondary">
                默认值为 32。成功、失败和重复的工具调用都会计入整个 Agent 工作流的上限；保存后仅影响新建或恢复的运行。
              </Typography.Paragraph>
            </Form.Item>
          </Form>
        )}
        {section === "provider_add" && (
          <Form layout="vertical">
            <Typography.Title level={4}>添加提供商</Typography.Title>
            <Space.Compact block>
              <Form.Item label="协议" style={{ flex: 1 }}>
                <Select
                  value={providerAddDraft.protocol}
                  options={[
                    { value: "chat_completions", label: "Chat Completions" },
                    { value: "responses", label: "Responses" },
                    { value: "messages", label: "Messages" },
                  ]}
                  onChange={(protocol) => setProviderAddDraft((current) => ({ ...current, protocol }))}
                />
              </Form.Item>
              <Form.Item label="提供商" style={{ flex: 1 }}>
                <Input
                  value={providerAddDraft.provider}
                  onChange={(event) => setProviderAddDraft((current) => ({ ...current, provider: event.target.value }))}
                />
              </Form.Item>
            </Space.Compact>
            <Form.Item label="Base URL">
              <Input
                value={providerAddDraft.base_url}
                onChange={(event) => setProviderAddDraft((current) => ({ ...current, base_url: event.target.value }))}
              />
            </Form.Item>
            <Form.Item label="模型">
              <AutoComplete
                options={matchingModels("new", providerAddDraft.model)}
                value={providerAddDraft.model}
                onChange={(model) => setProviderAddDraft((current) => ({ ...current, model }))}
                placeholder="手动输入或先获取模型列表"
              />
              <Button
                type="link"
                loading={modelsLoading.new}
                onClick={() => void discoverModels("new", {
                  provider: providerAddDraft.provider,
                  protocol: providerAddDraft.protocol,
                  base_url: providerAddDraft.base_url,
                  api_key: providerAddDraft.api_key,
                })}
              >获取 /v1/models</Button>
            </Form.Item>
            <Space.Compact block>
              <Form.Item label="最大输出 token" style={{ flex: 1 }}>
                <Input
                  type="number"
                  value={providerAddDraft.max_tokens}
                  onChange={(event) => setProviderAddDraft((current) => ({ ...current, max_tokens: Number(event.target.value) }))}
                />
              </Form.Item>
              <Form.Item label="上下文大小" style={{ flex: 1 }}>
                <Input
                  type="number"
                  value={providerAddDraft.context_size}
                  onChange={(event) => setProviderAddDraft((current) => ({ ...current, context_size: Number(event.target.value) }))}
                />
              </Form.Item>
            </Space.Compact>
            <Form.Item label="API Key">
              <Input.Password
                placeholder="输入 API Key"
                value={providerAddDraft.api_key}
                onChange={(event) => setProviderAddDraft((current) => ({ ...current, api_key: event.target.value }))}
              />
            </Form.Item>
          </Form>
        )}
        {section === "provider_manage" && (
          <div className="provider-management">
            <Typography.Title level={4}>提供商管理</Typography.Title>
            {(settings.provider_configs ?? []).length === 0 ? (
              <Typography.Text type="secondary">暂无提供商，请先在“添加提供商”中创建。</Typography.Text>
            ) : (
              <Collapse
                items={(settings.provider_configs ?? []).map((provider) => {
                  const draft = providerDrafts[provider.id] ?? { model: provider.model, api_key: "" };
                  return {
                    key: provider.id,
                    label: <span>{provider.provider} · {provider.model || "未选择模型"} {provider.is_active ? <Tag color="green">当前使用</Tag> : null}</span>,
                    children: (
                      <Form layout="vertical">
                        <Form.Item label="Base URL"><Input value={provider.base_url} disabled /></Form.Item>
                        <Form.Item label="模型">
                          <AutoComplete
                            options={matchingModels(provider.id, draft.model)}
                            value={draft.model}
                            onChange={(model) => updateProviderDraft(provider.id, { model })}
                            placeholder="手动输入或先获取模型列表"
                          />
                          <Button
                            type="link"
                            loading={modelsLoading[provider.id]}
                            onClick={() => void discoverModels(provider.id, {
                              provider: provider.provider,
                              protocol: provider.protocol,
                              base_url: provider.base_url,
                              api_key: draft.api_key,
                            })}
                          >获取 /v1/models</Button>
                        </Form.Item>
                        <Form.Item label="API Key">
                          <Input.Password
                            value={draft.api_key}
                            placeholder={provider.api_key_configured ? "已配置，留空以保持不变" : "输入 API Key"}
                            onChange={(event) => updateProviderDraft(provider.id, { api_key: event.target.value })}
                          />
                        </Form.Item>
                        <Space>
                          <Button type="primary" loading={saving} onClick={() => void saveManagedProvider(provider)}>保存修改</Button>
                          {!provider.is_active ? <Button onClick={() => void activateProvider(provider)}>设为当前使用</Button> : null}
                          <Button danger onClick={() => confirmDeleteProvider(provider)}>删除</Button>
                        </Space>
                      </Form>
                    ),
                  };
                })}
              />
            )}
          </div>
        )}
        {section === "cloud" && (
          <div className="cloud-sync-settings">
            <Typography.Title level={4}>云同步</Typography.Title>
            {user?.kind === "guest" ? (
              <Alert
                type="info"
                showIcon
                title="游客数据仅保存在本机"
                description="登录正式账户后才能保存和恢复云端版本。"
              />
            ) : null}
            <Typography.Paragraph type="secondary">
              云端保存包含用户设置、提供商密钥、会话、workspace、上传文件和 Skills；Benchmark、日志和缓存不会上传。
            </Typography.Paragraph>
            <Form layout="vertical" disabled={!cloudAvailable}>
              <Form.Item label="自动保存">
                <Switch
                  aria-label="自动保存到云端"
                  checked={settings.sync_preferences.auto_save_enabled}
                  onChange={(auto_save_enabled) => updateSettings({
                    sync_preferences: { ...settings.sync_preferences, auto_save_enabled },
                  })}
                />
              </Form.Item>
              <Form.Item label="自动保存规则">
                <Select
                  aria-label="自动保存规则"
                  disabled={!settings.sync_preferences.auto_save_enabled}
                  value={settings.sync_preferences.auto_save_rule}
                  options={[
                    { value: "idle_5m", label: "本地变化后空闲 5 分钟" },
                    { value: "after_run", label: "每次 Agent 运行结束后" },
                    { value: "hourly", label: "有修改时每小时" },
                  ]}
                  onChange={(auto_save_rule) => updateSettings({
                    sync_preferences: { ...settings.sync_preferences, auto_save_rule },
                  })}
                />
              </Form.Item>
            </Form>

            {cloudAvailable && (settings.sync_state.status === "conflict" || syncJob?.status === "conflict") ? (
              <Alert
                type="warning"
                showIcon
                title="本地数据与云端最新版本冲突"
                description="请选择上传本地数据生成新版本，或使用一个云端版本覆盖本地。"
                action={<Space wrap>
                  <Button onClick={() => void startCloudSave(true)}>使用本地数据</Button>
                  {cloudSnapshots[0] ? (
                    <Button danger onClick={() => confirmRestore(cloudSnapshots[0])}>使用最新云端版本</Button>
                  ) : null}
                </Space>}
              />
            ) : null}

            {cloudAvailable && syncJob ? (
              <div className="cloud-sync-job" aria-live="polite">
                <Space>
                  <Tag color={syncJob.status === "complete" ? "green" : syncJob.status === "failed" ? "red" : "blue"}>
                    {syncJob.kind === "restore" ? "恢复" : "保存"}
                  </Tag>
                  <Typography.Text>{syncJob.phase}</Typography.Text>
                </Space>
                <Progress
                  percent={syncJob.progress}
                  status={syncJob.status === "failed" ? "exception" : syncJob.status === "complete" ? "success" : "active"}
                />
                {syncJob.error ? <Typography.Text type="danger">{syncJob.error}</Typography.Text> : null}
              </div>
            ) : null}

            {cloudAvailable ? <Space wrap className="cloud-sync-actions">
              <Button
                type="primary"
                loading={cloudLoading || syncJob?.status === "queued" || syncJob?.status === "running"}
                onClick={() => void startCloudSave(false)}
              >
                保存到云端
              </Button>
              <Typography.Text type="secondary">
                状态：{settings.sync_state.status} · 本地版本 {settings.sync_state.local_revision}
              </Typography.Text>
            </Space> : null}

            {cloudAvailable ? <><Typography.Title level={5}>最近云端版本</Typography.Title>
            <List
              locale={{ emptyText: "暂无云端版本" }}
              dataSource={cloudSnapshots}
              renderItem={(item) => (
                <List.Item
                  actions={[<Button key="restore" danger onClick={() => confirmRestore(item)}>恢复</Button>]}
                >
                  <List.Item.Meta
                    title={`版本 ${item.version}`}
                    description={`${new Date(item.completed_at).toLocaleString()} · ${formatBytes(item.archive_size)} · ${item.device_id}`}
                  />
                </List.Item>
              )}
            /></> : null}
          </div>
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
      mask={{ closable: true }}
      keyboard={false}
      onCancel={requestClose}
      footer={section === "provider_manage" ? null : (
        <Space>
          <Button
            type="primary"
            aria-label="保存"
            loading={saving}
            disabled={loading || !settings || (section === "cloud" && !cloudAvailable)}
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
