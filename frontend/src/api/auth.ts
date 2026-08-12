import type { AuthResponse, AuthUser } from "../types";
import { apiUrl } from "./base";
import { ApiError, errorFrom, jsonBody, requestJson } from "./request";
import type { SyncPreferences, SyncState } from "./sync";

export async function getCurrentUser(): Promise<AuthUser | null> {
  const res = await fetch(apiUrl("/api/auth/me"), { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) throw new ApiError(res.status, await errorFrom(res));
  const body = (await res.json()) as AuthResponse | AuthUser;
  return "user" in body ? body.user : body;
}

export interface UserProfile {
  display_name: string;
  agent_preferences: string;
}

export async function getProfile(): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/auth/profile");
}

export async function updateProfile(profile: UserProfile): Promise<UserProfile> {
  return requestJson<UserProfile>("/api/auth/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

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
  provider: string;
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

export interface RuntimeConfig {
  max_tool_calls: number;
}

export interface UserSettings {
  profile: UserProfile & { email: string };
  agent_config: AgentConfig;
  provider_config: ProviderConfig;
  provider_configs: ProviderConfig[];
  capability_config: Record<string, unknown>;
  runtime_config: RuntimeConfig;
  timezone_options: TimezoneOption[];
  sync_preferences: SyncPreferences;
  sync_state: SyncState;
  cloud_sync_available?: boolean;
}

export async function getSettings(): Promise<UserSettings> {
  return requestJson<UserSettings>("/api/auth/settings");
}

export async function updateAgentConfig(config: AgentConfig): Promise<AgentConfig> {
  return requestJson<AgentConfig>("/api/auth/agent-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function updateRuntimeConfig(config: RuntimeConfig): Promise<RuntimeConfig> {
  return requestJson<RuntimeConfig>("/api/auth/runtime-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function updateProviderConfig(
  config: Omit<ProviderConfig, "id" | "is_active" | "api_key_configured"> & { api_key?: string },
): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/auth/provider-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function addProviderConfig(
  config: Omit<ProviderConfig, "id" | "is_active" | "api_key_configured"> & { api_key?: string },
): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>("/api/auth/provider-configs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function updateProviderConfigById(
  id: string,
  values: { model?: string; api_key?: string },
): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/auth/provider-configs/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function activateProviderConfig(id: string): Promise<ProviderConfig> {
  return requestJson<ProviderConfig>(`/api/auth/provider-configs/${encodeURIComponent(id)}/active`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
  });
}

export async function deleteProviderConfig(id: string): Promise<ProviderConfig[]> {
  return requestJson<ProviderConfig[]>(`/api/auth/provider-configs/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function discoverProviderModels(values: {
  config_id?: string;
  provider: string;
  protocol: ProviderConfig["protocol"];
  base_url: string;
  api_key?: string;
}): Promise<{ models: string[] }> {
  return requestJson<{ models: string[] }>("/api/auth/provider-models/discover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function requestRegisterCode(email: string): Promise<void> {
  await requestJson("/api/auth/register/code", jsonBody({ email }));
}

export async function register(email: string, code: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>("/api/auth/register", jsonBody({ email, code, password }));
  return body.user;
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>("/api/auth/login", jsonBody({ email, password }));
  return body.user;
}

export async function guestLogin(): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>("/api/auth/guest", jsonBody({}));
  return body.user;
}

export interface GuestImportStatus {
  available: boolean;
  pending: { guest_id: string; status: "pending"; created_at: number; updated_at: number } | null;
}

export async function getGuestImportStatus(): Promise<GuestImportStatus> {
  return requestJson<GuestImportStatus>("/api/auth/guest-import");
}

export async function resolveGuestImport(decision: "import" | "dismiss"): Promise<{
  status: string;
  imported: string[];
  skipped: string[];
  count: number;
}> {
  return requestJson(`/api/auth/guest-import`, jsonBody({ decision }));
}

export async function requestPasswordResetCode(email: string): Promise<void> {
  await requestJson("/api/auth/password-reset/code", jsonBody({ email }));
}

export async function resetPassword(email: string, code: string, password: string): Promise<AuthUser> {
  const body = await requestJson<AuthResponse>(
    "/api/auth/password-reset/confirm",
    jsonBody({ email, code, password }),
  );
  return body.user;
}

export async function logout(): Promise<void> {
  await requestJson("/api/auth/logout", jsonBody({}));
}

export interface DeviceStart {
  poll_secret: string;
  verification_url: string;
  expires_in: number;
  poll_interval: number;
}

export async function startDeviceAuthorization(): Promise<DeviceStart> {
  return requestJson<DeviceStart>("/api/auth/device/start", jsonBody({}));
}

export async function deviceInfo(grant: string): Promise<{ server_url: string; created_at: number; status: string }> {
  return requestJson(`/api/auth/device/info?grant=${encodeURIComponent(grant)}`);
}

export async function approveDevice(grant: string, approved: boolean): Promise<void> {
  await requestJson("/api/auth/device/approve", jsonBody({ grant, approved }));
}
