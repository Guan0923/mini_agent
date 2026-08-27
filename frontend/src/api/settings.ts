import type { LocalProfile } from "../types";
import { requestJson } from "./request";

export type UserProfile = LocalProfile;

export interface AgentConfig {
  tone: string;
  verbosity: string;
  initiative: string;
  custom_instructions: string;
  display_mode: "minimal" | "medium" | "verbose" | "developer";
  timezone: string;
  location_enabled: boolean;
}

export interface ProviderConfig {
  id: string;
  is_active: boolean;
  provider_name: string;
  protocol: "chat_completions" | "responses" | "messages";
  base_url: string;
  model: string;
  max_tokens: number;
  context_size: number;
  tokenizer_model: string;
  api_key_configured: boolean;
}

export interface TimezoneOption {
  identifier: string;
  label: string;
}

export type TerminalType = "cmd" | "git_bash" | "powershell" | "pwsh" | "wsl";

export interface RuntimeConfig {
  max_tool_calls: number;
  terminal_type: TerminalType;
}

export type SandboxFileMode = "read_only" | "workspace_write" | "full_access";
export type SandboxNetworkMode = "no_network" | "restricted_network" | "full_network";

export interface SandboxLimits {
  wall_seconds: number;
  cpu_seconds: number;
  memory_mib: number;
  processes: number;
  handles: number;
  output_chars: number;
  disk_mib: number;
}

export interface SandboxNetworkRule {
  host: string;
  port: number;
}

export interface SandboxConfig {
  enabled: true;
  file_mode: SandboxFileMode;
  network_mode: SandboxNetworkMode;
  network_allowlist: SandboxNetworkRule[];
  limits: SandboxLimits;
  full_access_acknowledged?: boolean;
}

export interface TerminalOption {
  value: TerminalType;
  label: string;
}

export interface UserSettings {
  profile: UserProfile;
  agent_config: AgentConfig;
  provider_config: ProviderConfig;
  provider_configs: ProviderConfig[];
  capability_config: Record<string, unknown>;
  runtime_config: RuntimeConfig;
  sandbox_config: SandboxConfig;
  terminal_options: TerminalOption[];
  terminal_notice: string | null;
  timezone_options: TimezoneOption[];
}

export function getSettings(): Promise<UserSettings> {
  return requestJson<UserSettings>("/api/settings");
}

export function getProfile(): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/settings/profile");
}

export function updateProfile(profile: UserProfile): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/settings/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export function updateAgentConfig(config: AgentConfig): Promise<AgentConfig> {
  return requestJson<AgentConfig>("/api/settings/agent", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateRuntimeConfig(config: RuntimeConfig): Promise<RuntimeConfig> {
  return requestJson<RuntimeConfig>("/api/settings/runtime", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateSandboxConfig(config: SandboxConfig): Promise<SandboxConfig> {
  return requestJson<SandboxConfig>("/api/settings/sandbox", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

type ProviderInput = Omit<ProviderConfig, "id" | "is_active" | "api_key_configured"> & { api_key?: string };

export function updateProviderConfig(config: ProviderInput): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/settings/providers", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function addProviderConfig(config: ProviderInput): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/settings/providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function updateProviderConfigById(
  id: string,
  values: { provider_name?: string; model?: string; api_key?: string },
): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/settings/providers/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export function activateProviderConfig(id: string): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/settings/providers/${encodeURIComponent(id)}/active`, {
    method: "PUT",
  });
}

export function deleteProviderConfig(id: string): Promise<ProviderConfig[]> {
  return requestJson<ProviderConfig[]>(`/api/settings/providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export function discoverProviderModels(values: {
  config_id?: string;
  provider_name: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  api_key?: string;
}): Promise<{ models: string[] }> {
  return requestJson<{ models: string[] }>("/api/settings/providers/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}
