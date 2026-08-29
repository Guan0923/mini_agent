import type { AgentConfig, ProviderConfig, SandboxConfig, UserSettings } from "../../api";
import type { SandboxHealthState } from "../../app/useSandboxHealth";
import type { LocalProfile } from "../../types";

export type SettingsSection = "profile" | "agent" | "runtime" | "sandbox" | "provider_add" | "provider_manage";

export type ProviderDraft = {
  provider_name: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  model: string;
  max_tokens: number;
  context_size: number;
  tokenizer_model: string;
  api_key: string;
};

export type ProviderEditDraft = { provider_name: string; model: string; api_key: string };

export type ProviderModelFeedback = {
  status: "success" | "warning" | "error";
  message: string;
};

export interface UserSettingsModalProps {
  open: boolean;
  profile: LocalProfile;
  onClose: () => void;
  activeSessionId?: string;
  onAgentConfigUpdate?: (config: AgentConfig) => void;
  onProviderConfigUpdate?: (config: ProviderConfig) => void;
  onProfileChange: (profile: LocalProfile) => void;
  sandboxHealth: SandboxHealthState;
}

export const defaultAgent: AgentConfig = {
  tone: "balanced",
  verbosity: "balanced",
  initiative: "balanced",
  display_mode: "medium",
  timezone: "Asia/Shanghai",
  location_enabled: false,
  custom_instructions: "",
};

export const defaultProvider: ProviderConfig = {
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

export const defaultProviderDraft: ProviderDraft = {
  provider_name: defaultProvider.provider_name,
  protocol: defaultProvider.protocol,
  base_url: defaultProvider.base_url,
  model: defaultProvider.model,
  max_tokens: defaultProvider.max_tokens,
  context_size: defaultProvider.context_size,
  tokenizer_model: defaultProvider.tokenizer_model,
  api_key: "",
};

export const defaultSandboxConfig: SandboxConfig = {
  network_mode: "no_network",
  network_allowlist: [],
  proxy_port: 17831,
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

export function snapshot(value: unknown): string {
  return JSON.stringify(value ?? null);
}

export function normalizeSettings(next: UserSettings): UserSettings {
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
  return {
    ...next,
    provider_config: currentProvider,
    provider_configs: providers,
    runtime_config: {
      ...(next.runtime_config ?? { max_tool_calls: 32, terminal_type: "cmd" }),
      terminal_type: next.runtime_config?.terminal_type ?? "cmd",
    },
    sandbox_config: {
      ...defaultSandboxConfig,
      ...(next.sandbox_config ?? {}),
      limits: { ...defaultSandboxConfig.limits, ...(next.sandbox_config?.limits ?? {}) },
    },
    terminal_options: next.terminal_options ?? [],
    terminal_notice: next.terminal_notice ?? null,
  };
}

export function fallbackSettings(profile: LocalProfile): UserSettings {
  return {
    profile: { display_name: profile.display_name, agent_preferences: profile.agent_preferences },
    agent_config: defaultAgent,
    provider_config: defaultProvider,
    provider_configs: [],
    capability_config: {},
    runtime_config: { max_tool_calls: 32, terminal_type: "cmd" },
    sandbox_config: defaultSandboxConfig,
    terminal_options: [],
    terminal_notice: null,
    timezone_options: [],
  };
}
