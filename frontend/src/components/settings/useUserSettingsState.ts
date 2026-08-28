import { App as AntApp } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  activateProviderConfig,
  addProviderConfig,
  deleteProviderConfig,
  discoverProviderModels,
  getSettings,
  setTimezone,
  updateAgentConfig,
  updateProfile,
  updateProviderConfigById,
  updateRuntimeConfig,
  updateSandboxConfig,
  type ProviderConfig,
  type UserSettings,
} from "../../api";
import {
  defaultProvider,
  defaultProviderDraft,
  fallbackSettings,
  normalizeSettings,
  snapshot,
  type ProviderDraft,
  type ProviderEditDraft,
  type ProviderModelFeedback,
  type SettingsSection,
  type UserSettingsModalProps,
} from "./contracts";

export function useUserSettingsState({
  open,
  profile,
  onClose,
  onProfileChange,
  activeSessionId,
  onAgentConfigUpdate,
  onProviderConfigUpdate,
  sandboxHealth,
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
  const [providerDrafts, setProviderDrafts] = useState<Record<string, ProviderEditDraft>>({});
  const [savedProviderDrafts, setSavedProviderDrafts] = useState<Record<string, ProviderEditDraft>>({});
  const [modelOptions, setModelOptions] = useState<Record<string, string[]>>({});
  const [modelsLoading, setModelsLoading] = useState<Record<string, boolean>>({});
  const [managedModelQueries, setManagedModelQueries] = useState<Record<string, string>>({});
  const [managedModelOpen, setManagedModelOpen] = useState<Record<string, boolean>>({});
  const [managedModelFeedback, setManagedModelFeedback] = useState<Record<string, ProviderModelFeedback>>({});
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
        const normalized = normalizeSettings(next);
        const drafts = Object.fromEntries(normalized.provider_configs.map((provider) => [
          provider.id,
          { provider_name: provider.provider_name, model: provider.model, api_key: "" },
        ]));
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
      })
      .catch((cause) => {
        if (!mounted) return;
        const fallback = fallbackSettings(profile);
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
  }, [open, profile.display_name, profile.agent_preferences]);

  const dirty = useMemo(
    () => snapshot({ settings, providerAddDraft, providerDrafts }) !== snapshot({
      settings: saved,
      providerAddDraft: savedProviderAddDraft,
      providerDrafts: savedProviderDrafts,
    }),
    [saved, savedProviderAddDraft, settings, providerAddDraft, providerDrafts, savedProviderDrafts],
  );

  function updateSettings(patch: Partial<UserSettings>) {
    setSettings((current) => (current ? { ...current, ...patch } : current));
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
        const nextProfile = await updateProfile({
          display_name: settings.profile.display_name.trim(),
          agent_preferences: settings.profile.agent_preferences.trim(),
        });
        updateSettings({ profile: { ...settings.profile, ...nextProfile } });
        setSaved((current) => current ? { ...current, profile: { ...current.profile, ...nextProfile } } : current);
        onProfileChange(nextProfile);
      } else if (section === "agent") {
        const agent = await updateAgentConfig(settings.agent_config);
        if (activeSessionId && saved?.agent_config.timezone !== settings.agent_config.timezone) {
          await setTimezone(activeSessionId, settings.agent_config.timezone);
        }
        updateSettings({ agent_config: agent });
        setSaved((current) => current ? { ...current, agent_config: agent } : current);
        onAgentConfigUpdate?.(agent);
      } else if (section === "runtime") {
        const runtime = await updateRuntimeConfig(settings.runtime_config);
        updateSettings({ runtime_config: runtime });
        setSaved((current) => current ? { ...current, runtime_config: runtime } : current);
      } else if (section === "sandbox") {
        const firstFullAccessSave = settings.sandbox_config.file_mode === "full_access"
          && saved?.sandbox_config.file_mode !== "full_access";
        if (firstFullAccessSave) {
          const confirmed = await new Promise<boolean>((resolve) => {
            modal.confirm({
              title: "启用 Full access？",
              content: "Full access 同时开放完整文件与网络访问，沙箱不再提供低权限隔离。请确认你理解此风险。",
              okText: "确认并保存",
              cancelText: "取消",
              okButtonProps: { danger: true },
              onOk: () => resolve(true),
              onCancel: () => resolve(false),
            });
          });
          if (!confirmed) return;
        }
        const sandbox = await updateSandboxConfig({
          ...settings.sandbox_config,
          ...(firstFullAccessSave ? { full_access_acknowledged: true } : {}),
        });
        updateSettings({ sandbox_config: sandbox });
        setSaved((current) => current ? { ...current, sandbox_config: sandbox } : current);
      } else if (section === "provider_add") {
        const provider = await addProviderConfig(providerAddDraft);
        const providers = [...settings.provider_configs, provider];
        updateSettings({ provider_configs: providers, ...(provider.is_active ? { provider_config: provider } : {}) });
        setSaved((current) => current ? {
          ...current,
          provider_configs: providers,
          ...(provider.is_active ? { provider_config: provider } : {}),
        } : current);
        if (provider.is_active) onProviderConfigUpdate?.(provider);
        setProviderAddDraft(defaultProviderDraft);
        setSavedProviderAddDraft(defaultProviderDraft);
        setModelOptions((current) => { const next = { ...current }; delete next.new; return next; });
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

  function updateProviderDraft(id: string, patch: Partial<ProviderEditDraft>) {
    setProviderDrafts((current) => ({
      ...current,
      [id]: { ...(current[id] ?? { provider_name: "", model: "", api_key: "" }), ...patch },
    }));
  }

  function matchingModels(id: string, input: string): { value: string }[] {
    const query = input.trim().toLowerCase();
    return (modelOptions[id] ?? [])
      .filter((model) => !query || model.toLowerCase().includes(query))
      .map((model) => ({ value: model }));
  }

  async function discoverModels(
    id: string,
    values: { provider_name: string; protocol: ProviderConfig["protocol"]; base_url: string; api_key?: string },
  ) {
    const managed = id !== "new";
    setModelsLoading((current) => ({ ...current, [id]: true }));
    if (managed) {
      setManagedModelQueries((current) => ({ ...current, [id]: "" }));
      setManagedModelOpen((current) => ({ ...current, [id]: false }));
      setManagedModelFeedback((current) => { const next = { ...current }; delete next[id]; return next; });
    } else {
      setError("");
    }
    try {
      const result = await discoverProviderModels({ ...values, ...(managed ? { config_id: id } : {}) });
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
          [id]: { status: "error", message: cause instanceof Error ? cause.message : "获取模型列表失败。" },
        }));
      } else {
        setError(cause instanceof Error ? cause.message : "获取模型列表失败。");
      }
    } finally {
      if (!managed || settingsOpenRef.current) setModelsLoading((current) => ({ ...current, [id]: false }));
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
      setSaved((current) => current ? {
        ...current,
        provider_configs: providers,
        provider_config: updated.is_active ? updated : current.provider_config,
      } : current);
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
        setSaved((current) => current ? { ...current, provider_configs: providers, provider_config: active } : current);
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
      setSaved((current) => current ? { ...current, provider_configs: providers, provider_config: active } : current);
      onProviderConfigUpdate?.(active);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "切换当前提供商失败。");
    } finally {
      setSaving(false);
    }
  }

  return {
    section,
    setSection,
    settings,
    loading,
    saving,
    error,
    locationError,
    setLocationError,
    providerAddDraft,
    setProviderAddDraft,
    providerDrafts,
    modelOptions,
    modelsLoading,
    managedModelQueries,
    setManagedModelQueries,
    managedModelOpen,
    setManagedModelOpen,
    managedModelFeedback,
    sandboxHealth,
    updateSettings,
    requestClose,
    toggleLocation,
    saveCurrent,
    updateProviderDraft,
    matchingModels,
    discoverModels,
    saveManagedProvider,
    confirmDeleteProvider,
    activateProvider,
  };
}

export type UserSettingsState = ReturnType<typeof useUserSettingsState>;
