import {
  App as AntApp,
  Alert,
  Button,
  Card,
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
import { useEffect, useMemo, useRef, useState } from "react";
import {
  getSettings,
  getSandboxStatus,
  installSandboxBroker,
  repairSandboxBroker,
  addProviderConfig,
  activateProviderConfig,
  deleteProviderConfig,
  discoverProviderModels,
  getSyncJob,
  getSyncStatus,
  syncNow,
  setTimezone,
  updateAgentConfig,
  updateProfile,
  updateRuntimeConfig,
  updateSandboxConfig,
  updateRagConfig,
  updateProviderConfigById,
  updateSyncPreferences,
  type AgentConfig,
  type ProviderConfig,
  type UserSettings,
  type RuntimeConfig,
  type SandboxBrokerStatus,
  type SandboxConfig,
  type RagConfig,
  type RagCapabilities,
  type SyncJob,
} from "../api";
import type { AuthUser } from "../types";
import KnowledgeBaseContent from "./KnowledgeBaseContent";

type SettingsSection = "profile" | "agent" | "runtime" | "sandbox" | "rag" | "rag_content" | "provider_add" | "provider_manage" | "cloud";

type ProviderDraft = {
  provider_name: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  model: string;
  max_tokens: number;
  context_size: number;
  tokenizer_model: string;
  api_key: string;
};

type ProviderModelFeedback = {
  status: "success" | "warning" | "error";
  message: string;
};

interface UserSettingsModalProps {
  open: boolean;
  user: AuthUser | null;
  onClose: () => void;
  activeSessionId?: string;
  onAgentConfigUpdate?: (config: AgentConfig) => void;
  onProviderConfigUpdate?: (config: ProviderConfig) => void;
  onRagConfigUpdate?: (config: RagConfig) => void;
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
  provider_name: "default",
  protocol: "chat_completions",
  base_url: "",
  model: "",
  max_tokens: 8192,
  context_size: 1024000,
  tokenizer_model: "",
  api_key_configured: false,
};

const defaultProviderDraft: ProviderDraft = {
  provider_name: defaultProvider.provider_name,
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
  cloud_revision: 0,
  pending_event_count: 0,
  status: "local_only" as const,
  last_error: "",
  updated_at: null,
};

const defaultRagConfig: RagConfig = {
  enabled: false,
  algorithm: "hybrid",
  bm25_candidate_k: 20,
  vector_candidate_k: 20,
  top_k: 8,
  embedding_base_url: "http://127.0.0.1:11434",
  embedding_model: "bge-m3",
};

const defaultSandboxConfig: SandboxConfig = {
  enabled: false,
  file_mode: "read_only",
  network_mode: "no_network",
  network_allowlist: [],
  limits: {
    wall_seconds: 300,
    cpu_seconds: 300,
    memory_mib: 4096,
    processes: 256,
    handles: 16384,
    output_chars: 20000,
    disk_mib: 0,
  },
};

function snapshot(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export default function UserSettingsModal({
  open,
  user,
  onClose,
  onUserUpdate,
  activeSessionId,
  onAgentConfigUpdate,
  onProviderConfigUpdate,
  onRagConfigUpdate,
}: UserSettingsModalProps) {
  const { modal, message } = AntApp.useApp();
  const [section, setSection] = useState<SettingsSection>("profile");
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [saved, setSaved] = useState<UserSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [locationError, setLocationError] = useState("");
  const [providerAddDraft, setProviderAddDraft] = useState<ProviderDraft>(defaultProviderDraft);
  const [savedProviderAddDraft, setSavedProviderAddDraft] = useState<ProviderDraft>(defaultProviderDraft);
  const [providerDrafts, setProviderDrafts] = useState<Record<string, { provider_name: string; model: string; api_key: string }>>({});
  const [savedProviderDrafts, setSavedProviderDrafts] = useState<Record<string, { provider_name: string; model: string; api_key: string }>>({});
  const [modelOptions, setModelOptions] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState<Record<string, boolean>>({});
  const [managedModelQueries, setManagedModelQueries] = useState<Record<string, string>>({});
  const [managedModelOpen, setManagedModelOpen] = useState<Record<string, boolean>>({});
  const [managedModelFeedback, setManagedModelFeedback] = useState<Record<string, ProviderModelFeedback>>({});
  const [syncJob, setSyncJob] = useState<SyncJob | null>(null);
  const [cloudLoading, setCloudLoading] = useState(false);
  const [ragCapabilities, setRagCapabilities] = useState<RagCapabilities | null>(null);
  const [brokerStatus, setBrokerStatus] = useState<SandboxBrokerStatus | null>(null);
  const [brokerAction, setBrokerAction] = useState<"install" | "repair" | null>(null);
  const [sandboxHostDraft, setSandboxHostDraft] = useState("");
  const [sandboxPortDraft, setSandboxPortDraft] = useState<number | null>(443);
  const settingsOpenRef = useRef(open);

  useEffect(() => {
    settingsOpenRef.current = open;
    if (open) return;
    setManagedModelQueries({});
    setManagedModelOpen({});
    setManagedModelFeedback({});
    setModelsLoading({});
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let mounted = true;
    setLoading(true);
    setError("");
    void getSettings()
      .then((next) => {
        if (!mounted) return;
        // v0.3 responses use provider_name.  Accepting the pre-v0.3 provider
        // key at this UI boundary keeps older cached settings renderable while
        // all writes continue to use the new provider_name contract.
        const rawProviders = next.provider_configs ?? (next.provider_config?.id ? [next.provider_config] : []);
        const providers = rawProviders.map((provider) => {
          const providerName = provider.provider_name || (provider as ProviderConfig & { provider?: string }).provider || "default";
          return {
            ...provider,
            provider_name: providerName.toLowerCase() === "deepseek" ? "default" : providerName,
          };
        });
        const currentProvider = providers.find((provider) => provider.is_active)
          ?? (next.provider_config?.id ? next.provider_config : defaultProvider);
        const normalized = {
          ...next,
          provider_config: currentProvider,
          provider_configs: providers,
          sync_preferences: next.sync_preferences ?? defaultSyncPreferences,
          sync_state: next.sync_state ?? defaultSyncState,
          runtime_config: {
            ...(next.runtime_config ?? { max_tool_calls: 32, terminal_type: "cmd" }),
            terminal_type: next.runtime_config?.terminal_type ?? "cmd",
          },
          rag_config: { ...defaultRagConfig, ...(next.rag_config ?? {}) },
          sandbox_config: {
            ...defaultSandboxConfig,
            ...(next.sandbox_config ?? {}),
            limits: { ...defaultSandboxConfig.limits, ...(next.sandbox_config?.limits ?? {}) },
          },
          terminal_options: next.terminal_options ?? [],
          terminal_notice: next.terminal_notice ?? null,
        };
        const drafts = Object.fromEntries(providers.map((provider) => [provider.id, { provider_name: provider.provider_name, model: provider.model, api_key: "" }]));
        setSettings(normalized);
        setSaved(normalized);
        setProviderAddDraft(defaultProviderDraft);
        setSavedProviderAddDraft(defaultProviderDraft);
        setProviderDrafts(drafts);
        setSavedProviderDrafts(drafts);
        setModelOptions({});
        setModelsLoading({});
        setManagedModelQueries({});
        setManagedModelOpen({});
        setManagedModelFeedback({});
        setRagCapabilities(null);
        if (typeof getSandboxStatus === "function") {
          void getSandboxStatus().then(setBrokerStatus).catch(() => setBrokerStatus(null));
        }
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
          runtime_config: { max_tool_calls: 32, terminal_type: "cmd" },
          rag_config: defaultRagConfig,
          sandbox_config: defaultSandboxConfig,
          terminal_options: [],
          terminal_notice: null,
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
        setManagedModelQueries({});
        setManagedModelOpen({});
        setManagedModelFeedback({});
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
    if (!open || section !== "rag") return;
    let active = true;
    void fetch("/api/rag/capabilities", { credentials: "include" })
      .then((response) => response.ok ? response.json() as Promise<RagCapabilities> : Promise.reject(new Error("capabilities unavailable")))
      .then((capabilities) => { if (active) setRagCapabilities(capabilities); })
      .catch(() => { if (active) setRagCapabilities(null); });
    return () => { active = false; };
  }, [open, section]);

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
      const status = await getSyncStatus();
      setSyncJob(status.job);
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
      } else if (section === "sandbox") {
        if (settings.sandbox_config.file_mode === "full_access" && saved?.sandbox_config.file_mode !== "full_access") {
          const confirmed = await new Promise<boolean>((resolve) => {
            modal.confirm({
              title: "启用 Full access？",
              content: "这会同时放开工作区文件和网络访问，并标记为非沙箱运行。",
              okText: "继续",
              cancelText: "取消",
              onOk: () => resolve(true),
              onCancel: () => resolve(false),
            });
          });
          if (!confirmed) return;
        }
        const sandbox = await updateSandboxConfig({
          ...settings.sandbox_config,
          ...(settings.sandbox_config.file_mode === "full_access" ? { full_access_acknowledged: true } : {}),
        });
        updateSettings({ sandbox_config: sandbox });
        setSaved((current) => (current ? { ...current, sandbox_config: sandbox } : current));
      } else if (section === "rag") {
        const rag = await updateRagConfig(settings.rag_config ?? defaultRagConfig);
        updateSettings({ rag_config: rag });
        setSaved((current) => (current ? { ...current, rag_config: rag } : current));
        onRagConfigUpdate?.(rag);
      } else if (section === "provider_add") {
        const provider = await addProviderConfig({
          provider_name: providerAddDraft.provider_name,
          protocol: providerAddDraft.protocol,
          base_url: providerAddDraft.base_url,
          model: providerAddDraft.model,
          max_tokens: providerAddDraft.max_tokens,
          context_size: providerAddDraft.context_size,
          tokenizer_model: providerAddDraft.tokenizer_model,
          api_key: providerAddDraft.api_key,
        });
        const providers = [...(settings.provider_configs ?? []), provider];
        updateSettings({
          provider_configs: providers,
          ...(provider.is_active ? { provider_config: provider } : {}),
        });
        setSaved((current) => (current ? {
          ...current,
          provider_configs: providers,
          ...(provider.is_active ? { provider_config: provider } : {}),
        } : current));
        if (provider.is_active) onProviderConfigUpdate?.(provider);
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
      message.success("保存成功");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  }

  async function runBrokerAction(action: "install" | "repair") {
    setBrokerAction(action);
    try {
      const status = await (action === "install" ? installSandboxBroker() : repairSandboxBroker());
      setBrokerStatus(status);
    } catch (cause) {
      setBrokerStatus({
        installed: false,
        healthy: false,
        detail: cause instanceof Error ? cause.message : "Broker 操作失败。",
      });
    } finally {
      setBrokerAction(null);
    }
  }

  async function startCloudSave(force = false): Promise<void> {
    if (!cloudAvailable) return;
    setCloudLoading(true);
    setError("");
    try {
      const job = await syncNow(force);
      setSyncJob(job);
      await refreshCloud();
      message.success("同步已启动");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "启动云端同步失败。");
    } finally {
      setCloudLoading(false);
    }
  }


  function updateProviderDraft(id: string, patch: Partial<{ provider_name: string; model: string; api_key: string }>) {
    setProviderDrafts((current) => ({ ...current, [id]: { ...(current[id] ?? { provider_name: "", model: "", api_key: "" }), ...patch } }));
  }

  async function discoverModels(id: string, values: { provider_name: string; protocol: ProviderConfig["protocol"]; base_url: string; api_key?: string }) {
    const managed = id !== "new";
    setModelsLoading((current) => ({ ...current, [id]: true }));
    if (managed) {
      setManagedModelQueries((current) => ({ ...current, [id]: "" }));
      setManagedModelOpen((current) => ({ ...current, [id]: false }));
      setManagedModelFeedback((current) => {
        const next = { ...current };
        delete next[id];
        return next;
      });
    } else {
      setError("");
    }
    try {
      const result = await discoverProviderModels({ ...values, ...(id !== "new" ? { config_id: id } : {}) });
      if (managed && !settingsOpenRef.current) return;
      setModelOptions((current) => ({ ...current, [id]: result.models }));
      if (managed) {
        if (result.models.length === 0) {
          setManagedModelFeedback((current) => ({
            ...current,
            [id]: { status: "warning", message: "模型服务没有返回可用模型，请继续手动输入。" },
          }));
        } else {
          setManagedModelOpen((current) => ({ ...current, [id]: true }));
          setManagedModelFeedback((current) => ({
            ...current,
            [id]: { status: "success", message: `已获取 ${result.models.length} 个模型` },
          }));
        }
      } else if (result.models.length === 0) {
        setError("模型服务没有返回可用模型，请继续手动输入。");
      }
    } catch (cause) {
      if (managed) {
        if (!settingsOpenRef.current) return;
        setManagedModelFeedback((current) => ({
          ...current,
          [id]: {
            status: "error",
            message: cause instanceof Error ? cause.message : "获取模型列表失败。",
          },
        }));
      } else {
        setError(cause instanceof Error ? cause.message : "获取模型列表失败。");
      }
    } finally {
      if (!managed || settingsOpenRef.current) {
        setModelsLoading((current) => ({ ...current, [id]: false }));
      }
    }
  }

  async function saveManagedProvider(provider: ProviderConfig) {
    const draft = providerDrafts[provider.id] ?? { provider_name: provider.provider_name, model: provider.model, api_key: "" };
    setSaving(true);
    setError("");
    try {
      const updated = await updateProviderConfigById(provider.id, {
        provider_name: draft.provider_name,
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
      const nextDraft = { provider_name: updated.provider_name, model: updated.model, api_key: "" };
      setProviderDrafts((current) => ({ ...current, [updated.id]: nextDraft }));
      setSavedProviderDrafts((current) => ({ ...current, [updated.id]: nextDraft }));
      if (updated.is_active) onProviderConfigUpdate?.(updated);
      message.success("保存成功");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存提供商失败。");
    } finally {
      setSaving(false);
    }
  }

  function confirmDeleteProvider(provider: ProviderConfig) {
    modal.confirm({
      title: `删除提供商 ${provider.provider_name}？`,
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
        onProviderConfigUpdate?.(active);
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
      onProviderConfigUpdate?.(active);
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
    { key: "sandbox", label: "沙箱" },
    { key: "rag", label: "知识库配置" },
    { key: "rag_content", label: "知识库内容" },
    { key: "provider_add", label: "添加提供商" },
    { key: "provider_manage", label: "Provider 与模型" },
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
            <Form.Item label="用户名">
              <Input
                aria-label="用户名"
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
            {settings.terminal_notice ? (
              <Alert
                type="warning"
                showIcon
                title="终端状态提示"
                description={settings.terminal_notice}
                style={{ marginBottom: 16 }}
              />
            ) : null}
            <Form.Item label="启动终端">
              <Select
                aria-label="启动终端"
                value={settings.runtime_config.terminal_type}
                options={settings.terminal_options}
                disabled={settings.terminal_options.length === 0}
                onChange={(terminal_type) => updateSettings({
                  runtime_config: { ...settings.runtime_config, terminal_type },
                })}
                notFoundContent="暂无可用终端"
              />
              <Typography.Paragraph type="secondary">
                仅显示当前系统已安装且可调用的终端；保存后仅影响新建或恢复的运行。
              </Typography.Paragraph>
            </Form.Item>
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
                    updateSettings({ runtime_config: { ...settings.runtime_config, max_tool_calls } });
                  }
                }}
              />
              <Typography.Paragraph type="secondary">
                默认值为 32。成功、失败和重复的工具调用都会计入整个 Agent 工作流的上限；保存后仅影响新建或恢复的运行。
              </Typography.Paragraph>
            </Form.Item>
          </Form>
        )}
        {section === "sandbox" && (
          <Form layout="vertical">
            <Typography.Title level={4}>Windows 沙箱</Typography.Title>
            <Form.Item label="启用严格沙箱">
              <Switch
                checked={settings.sandbox_config.enabled}
                onChange={(enabled) => updateSettings({ sandbox_config: { ...settings.sandbox_config, enabled } })}
              />
            </Form.Item>
            <Form.Item label="文件权限">
              <Select
                aria-label="文件权限"
                value={settings.sandbox_config.file_mode}
                options={[
                  { value: "read_only", label: "只读工作区" },
                  { value: "workspace_write", label: "读写工作区" },
                  { value: "full_access", label: "Full access（高风险）" },
                ]}
                onChange={(file_mode) => updateSettings({
                  sandbox_config: {
                    ...settings.sandbox_config,
                    file_mode,
                    ...(file_mode === "full_access" ? { network_mode: "full_network" as const } : {}),
                  },
                })}
              />
            </Form.Item>
            <Form.Item label="网络权限">
              <Select
                aria-label="网络权限"
                value={settings.sandbox_config.network_mode}
                options={[
                  { value: "no_network", label: "禁止网络" },
                  { value: "restricted_network", label: "受限网络" },
                  { value: "full_network", label: "完整网络" },
                ]}
                disabled={settings.sandbox_config.file_mode === "full_access"}
                onChange={(network_mode) => updateSettings({ sandbox_config: { ...settings.sandbox_config, network_mode } })}
              />
            </Form.Item>
            <Form.Item label="受限网络白名单">
              <Space.Compact block>
                <Input
                  aria-label="白名单域名"
                  placeholder="example.com"
                  value={sandboxHostDraft}
                  onChange={(event) => setSandboxHostDraft(event.target.value)}
                />
                <InputNumber
                  aria-label="白名单端口"
                  min={1}
                  max={65535}
                  precision={0}
                  value={sandboxPortDraft}
                  onChange={setSandboxPortDraft}
                />
                <Button
                  onClick={() => {
                    const host = sandboxHostDraft.trim().toLowerCase();
                    if (!host || sandboxPortDraft == null || !Number.isInteger(sandboxPortDraft)) return;
                    const exists = settings.sandbox_config.network_allowlist.some(
                      (rule) => rule.host === host && rule.port === sandboxPortDraft,
                    );
                    if (exists) return;
                    updateSettings({
                      sandbox_config: {
                        ...settings.sandbox_config,
                        network_allowlist: [...settings.sandbox_config.network_allowlist, { host, port: sandboxPortDraft }],
                      },
                    });
                    setSandboxHostDraft("");
                  }}
                >添加</Button>
              </Space.Compact>
              <List
                size="small"
                dataSource={settings.sandbox_config.network_allowlist}
                locale={{ emptyText: "暂无白名单规则" }}
                renderItem={(rule) => (
                  <List.Item actions={[
                    <Button
                      key={`${rule.host}:${rule.port}`}
                      type="link"
                      danger
                      onClick={() => updateSettings({
                        sandbox_config: {
                          ...settings.sandbox_config,
                          network_allowlist: settings.sandbox_config.network_allowlist.filter(
                            (item) => item.host !== rule.host || item.port !== rule.port,
                          ),
                        },
                      })}
                    >删除</Button>,
                  ]}>
                    {rule.host}:{rule.port}
                  </List.Item>
                )}
              />
            </Form.Item>
            <Space wrap>
              <Form.Item label="墙钟秒数"><InputNumber aria-label="墙钟秒数" min={1} max={300} value={settings.sandbox_config.limits.wall_seconds} onChange={(wall_seconds) => wall_seconds != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, wall_seconds } } })} /></Form.Item>
              <Form.Item label="CPU 秒数"><InputNumber aria-label="CPU 秒数" min={1} max={300} value={settings.sandbox_config.limits.cpu_seconds} onChange={(cpu_seconds) => cpu_seconds != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, cpu_seconds } } })} /></Form.Item>
              <Form.Item label="内存 MiB"><InputNumber aria-label="内存 MiB" min={128} max={4096} value={settings.sandbox_config.limits.memory_mib} onChange={(memory_mib) => memory_mib != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, memory_mib } } })} /></Form.Item>
              <Form.Item label="进程数"><InputNumber aria-label="进程数" min={1} max={256} value={settings.sandbox_config.limits.processes} onChange={(processes) => processes != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, processes } } })} /></Form.Item>
              <Form.Item label="句柄数"><InputNumber aria-label="句柄数" min={64} max={16384} value={settings.sandbox_config.limits.handles} onChange={(handles) => handles != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, handles } } })} /></Form.Item>
              <Form.Item label="输出字符数"><InputNumber aria-label="输出字符数" min={1000} max={20000} value={settings.sandbox_config.limits.output_chars} onChange={(output_chars) => output_chars != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, output_chars } } })} /></Form.Item>
              <Form.Item label="磁盘写入 MiB"><InputNumber aria-label="磁盘写入 MiB" min={0} max={20480} value={settings.sandbox_config.limits.disk_mib} onChange={(disk_mib) => disk_mib != null && updateSettings({ sandbox_config: { ...settings.sandbox_config, limits: { ...settings.sandbox_config.limits, disk_mib } } })} /></Form.Item>
            </Space>
            <Alert
              type={brokerStatus?.healthy ? "success" : "warning"}
              showIcon
              title={brokerStatus?.healthy ? "Broker 已就绪" : "Broker 未就绪"}
              description={brokerStatus?.detail ?? "严格沙箱初始化失败时不会降级到普通进程。"}
              action={<Space><Button autoInsertSpace={false} size="small" loading={brokerAction === "install"} onClick={() => void runBrokerAction("install")}>安装</Button><Button autoInsertSpace={false} size="small" loading={brokerAction === "repair"} onClick={() => void runBrokerAction("repair")}>修复</Button></Space>}
            />
          </Form>
        )}
        {section === "rag" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16, width: "100%" }}>
            <Card title="知识库开关" size="small">
              <Form layout="vertical">
                <Form.Item label="启用知识库检索" extra="关闭只停用聊天检索，不会删除已导入的文件和索引。">
                  <Switch checked={settings.rag_config?.enabled ?? false} onChange={(enabled) => updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), enabled } })} />
                </Form.Item>
              </Form>
            </Card>
            <Card title="检索策略" size="small">
              <Form layout="vertical">
                <Form.Item label="算法">
                  <Select
                    value={settings.rag_config?.algorithm ?? "hybrid"}
                    disabled={!ragCapabilities || ragCapabilities.algorithms.length === 0}
                    options={(ragCapabilities?.algorithms ?? []).map((algorithm) => ({ value: algorithm, label: algorithm === "hybrid" ? "Hybrid" : algorithm === "bm25" ? "BM25" : "Vector" }))}
                    onChange={(algorithm) => updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), algorithm } })}
                  />
                </Form.Item>
                <Space wrap>
                  <Form.Item label="BM25 候选 K"><InputNumber min={1} max={100} step={1} precision={0} value={settings.rag_config?.bm25_candidate_k ?? 20} onChange={(value) => value != null && updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), bm25_candidate_k: value } })} /></Form.Item>
                  <Form.Item label="Vector 候选 K"><InputNumber min={1} max={100} step={1} precision={0} value={settings.rag_config?.vector_candidate_k ?? 20} onChange={(value) => value != null && updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), vector_candidate_k: value } })} /></Form.Item>
                  <Form.Item label="最终 Top-K"><InputNumber min={1} max={20} step={1} precision={0} value={settings.rag_config?.top_k ?? 8} onChange={(value) => value != null && updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), top_k: value } })} /></Form.Item>
                </Space>
                {ragCapabilities && ragCapabilities.algorithms.length === 0 ? <Alert type="warning" showIcon title="当前没有可用的检索算法" description="请检查 SQLite FTS5、Qdrant 和 Ollama 服务状态。" /> : null}
              </Form>
            </Card>
            <Card title="Embedding Profile" size="small">
              <Form layout="vertical">
                <Form.Item label="Ollama Base URL"><Input value={settings.rag_config?.embedding_base_url ?? defaultRagConfig.embedding_base_url} onChange={(event) => updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), embedding_base_url: event.target.value } })} /></Form.Item>
                <Form.Item label="Embedding 模型">
                  <Select
                    value={settings.rag_config?.embedding_model ?? "bge-m3"}
                    disabled={!ragCapabilities || ragCapabilities.embedding_models.length === 0}
                    options={(ragCapabilities?.embedding_models ?? []).map((model) => ({ value: model, label: model }))}
                    onChange={(embedding_model) => updateSettings({ rag_config: { ...(settings.rag_config ?? defaultRagConfig), embedding_model } })}
                    notFoundContent="暂无健康检查通过的模型"
                  />
                </Form.Item>
                <Typography.Paragraph type="secondary">维度：{ragCapabilities?.dimension ?? 1024} · 已导入文件：{ragCapabilities?.imported_files ?? 0} · Qdrant：{ragCapabilities?.qdrant_healthy ? "正常" : "不可用"} · Ollama：{ragCapabilities?.ollama_healthy ? "正常" : "不可用"}</Typography.Paragraph>
                <Typography.Paragraph type="secondary">固定切块规则：700 tokens / 100 overlap / jieba 预分词</Typography.Paragraph>
              </Form>
            </Card>
          </div>
        )}
        {section === "rag_content" && <KnowledgeBaseContent activeSessionId={activeSessionId} />}
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
              <Form.Item label="配置名称" style={{ flex: 1 }}>
                <Input
                  value={providerAddDraft.provider_name}
                  onChange={(event) => setProviderAddDraft((current) => ({ ...current, provider_name: event.target.value }))}
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
                  provider_name: providerAddDraft.provider_name,
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
            <Typography.Title level={4}>Provider 与模型</Typography.Title>
            {(settings.provider_configs ?? []).length === 0 ? (
              <Typography.Text type="secondary">暂无提供商，请先在“添加提供商”中创建。</Typography.Text>
            ) : (
              <Collapse
                items={(settings.provider_configs ?? []).map((provider) => {
                  const draft = providerDrafts[provider.id] ?? { provider_name: provider.provider_name, model: provider.model, api_key: "" };
                  const modelFeedback = managedModelFeedback[provider.id];
                  return {
                    key: provider.id,
                    label: <span>{provider.provider_name} · {provider.model || "未选择模型"} {provider.is_active ? <Tag color="green">当前使用</Tag> : null}</span>,
                    children: (
                      <Form layout="vertical">
                        <Form.Item label="配置名称"><Input value={draft.provider_name} onChange={(event) => updateProviderDraft(provider.id, { provider_name: event.target.value })} /></Form.Item>
                        <Form.Item label="Base URL"><Input value={provider.base_url} disabled /></Form.Item>
                        <Form.Item label="模型">
                          <AutoComplete
                            options={matchingModels(provider.id, managedModelQueries[provider.id] ?? "")}
                            value={draft.model}
                            onChange={(model) => updateProviderDraft(provider.id, { model })}
                            onSelect={(model) => {
                              updateProviderDraft(provider.id, { model });
                              setManagedModelQueries((current) => ({ ...current, [provider.id]: "" }));
                              setManagedModelOpen((current) => ({ ...current, [provider.id]: false }));
                            }}
                            open={managedModelOpen[provider.id] ?? false}
                            onOpenChange={(nextOpen) => setManagedModelOpen((current) => ({ ...current, [provider.id]: nextOpen }))}
                            showSearch={{
                              filterOption: false,
                              onSearch: (query) => setManagedModelQueries((current) => ({ ...current, [provider.id]: query })),
                            }}
                            placeholder="手动输入或先获取模型列表"
                          />
                          <Button
                            type="link"
                            loading={modelsLoading[provider.id]}
                            onClick={() => void discoverModels(provider.id, {
                              provider_name: provider.provider_name,
                              protocol: provider.protocol,
                              base_url: provider.base_url,
                              api_key: draft.api_key,
                            })}
                          >获取 /v1/models</Button>
                          {modelFeedback ? (
                            <Typography.Text
                              aria-live="polite"
                              type={modelFeedback.status === "error" ? "danger" : modelFeedback.status}
                              style={{ display: "block", marginTop: 4 }}
                            >
                              {modelFeedback.message}
                            </Typography.Text>
                          ) : null}
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
                description="登录正式账户后才能同步加密的会话事件。"
              />
            ) : null}
            <Typography.Paragraph type="secondary">
              云端只同步加密的日志、消息、运行状态、checkpoint 和同步元数据；workspace、上传文件、Skills、RAG、插件与 MCP 始终保留在本机。
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
                 description="同步会保留完整事件历史；请先拉取远端增量后再重试。"
                 action={<Space wrap>
                   <Button onClick={() => void startCloudSave(true)}>重试同步</Button>
                 </Space>}
              />
            ) : null}

            {cloudAvailable && syncJob ? (
              <div className="cloud-sync-job" aria-live="polite">
                <Space>
                  <Tag color={syncJob.status === "complete" ? "green" : syncJob.status === "failed" ? "red" : "blue"}>
                    同步
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
                立即同步
              </Button>
              <Typography.Text type="secondary">
                状态：{settings.sync_state.status} · 本地 revision {settings.sync_state.local_revision} · 云端 revision {settings.sync_state.cloud_revision} · 待同步事件 {settings.sync_state.pending_event_count}
              </Typography.Text>
            </Space> : null}

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
      footer={["provider_manage", "rag_content"].includes(section) ? null : (
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
