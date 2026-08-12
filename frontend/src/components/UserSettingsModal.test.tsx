import { App as AntApp } from "antd";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UserSettingsModal from "./UserSettingsModal";

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateProfile: vi.fn(),
  updateAgentConfig: vi.fn(),
  updateProviderConfig: vi.fn(),
  addProviderConfig: vi.fn(),
  updateProviderConfigById: vi.fn(),
  activateProviderConfig: vi.fn(),
  deleteProviderConfig: vi.fn(),
  discoverProviderModels: vi.fn(),
  setTimezone: vi.fn(),
  getSyncStatus: vi.fn(),
  getCloudSnapshots: vi.fn(),
  getSyncJob: vi.fn(),
  updateSyncPreferences: vi.fn(),
  saveToCloud: vi.fn(),
  restoreCloudSnapshot: vi.fn(),
}));

vi.mock("../api", () => api);

const settings = {
  profile: { email: "user@example.com", display_name: "旧名字", agent_preferences: "" },
  agent_config: { tone: "balanced", verbosity: "balanced", initiative: "balanced", custom_instructions: "" },
  provider_config: {
    id: "provider-1",
    is_active: true,
    provider: "openai",
    protocol: "chat_completions" as const,
    base_url: "https://example.test/v1",
    model: "demo",
    max_tokens: 8192,
    context_size: 1024000,
    tokenizer_model: "demo",
    api_key_configured: false,
  },
  provider_configs: [{
    id: "provider-1",
    is_active: true,
    provider: "openai",
    protocol: "chat_completions" as const,
    base_url: "https://example.test/v1",
    model: "demo",
    max_tokens: 8192,
    context_size: 1024000,
    tokenizer_model: "demo",
    api_key_configured: false,
  }],
  capability_config: {},
  timezone_options: [],
  sync_preferences: { auto_save_enabled: false, auto_save_rule: "idle_5m" as const },
  sync_state: {
    local_revision: 2,
    uploaded_revision: 1,
    cloud_snapshot_id: null,
    status: "dirty" as const,
    last_error: "",
    updated_at: 1,
  },
};

function renderModal(onClose = vi.fn(), onUserUpdate = vi.fn()) {
  return render(
    <AntApp>
      <UserSettingsModal
        open
        user={{ id: "u1", email: "user@example.com", kind: "account", display_name: "user@example.com" }}
        onClose={onClose}
        onUserUpdate={onUserUpdate}
      />
    </AntApp>,
  );
}

describe("UserSettingsModal", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getSettings.mockResolvedValue(structuredClone(settings));
    api.updateProfile.mockResolvedValue({ display_name: "新名字", agent_preferences: "" });
    api.updateAgentConfig.mockResolvedValue(settings.agent_config);
    api.updateProviderConfig.mockResolvedValue(settings.provider_config);
    api.getSyncStatus.mockResolvedValue({
      preferences: settings.sync_preferences,
      state: settings.sync_state,
      job: null,
    });
    api.getCloudSnapshots.mockResolvedValue([]);
    api.updateSyncPreferences.mockImplementation(async (value) => value);
    api.saveToCloud.mockResolvedValue({
      id: "job-1",
      kind: "save",
      status: "queued",
      phase: "queued",
      progress: 0,
      snapshot_id: null,
      error: "",
      created_at: 1,
      updated_at: 1,
    });
  });

  it("switches among profile, agent, and provider sections", async () => {
    renderModal();
    expect(await screen.findByDisplayValue("user@example.com")).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveValue("旧名字");

    await userEvent.click(screen.getByRole("menuitem", { name: "Agent 配置" }));
    expect(screen.getByRole("textbox", { name: "自由文本偏好" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "添加提供商" }));
    expect(screen.getByText("Base URL")).toBeInTheDocument();
    expect(screen.getByText("Chat Completions")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "提供商管理" }));
    expect(screen.getByText(/openai/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "云同步" }));
    expect(screen.getByRole("switch", { name: "自动保存到云端" })).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "自动保存规则" })).toBeDisabled();
  });

  it("saves cloud preferences and starts a background cloud snapshot", async () => {
    renderModal();
    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "云同步" }));
    await userEvent.click(screen.getByRole("switch", { name: "自动保存到云端" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.updateSyncPreferences).toHaveBeenCalledWith({
      auto_save_enabled: true,
      auto_save_rule: "idle_5m",
    }));
    await userEvent.click(screen.getByRole("button", { name: "保存到云端" }));
    await waitFor(() => expect(api.saveToCloud).toHaveBeenCalledWith(false));
  });

  it("checks dirty state for mask and close button but keeps Escape disabled", async () => {
    const onClose = vi.fn();
    renderModal(onClose);
    await screen.findByDisplayValue("旧名字");

    const mask = document.querySelector(".ant-modal-mask");
    const modalWrap = document.querySelector(".ant-modal-wrap");
    if (!mask || !modalWrap) throw new Error("modal mask not rendered");
    fireEvent.mouseDown(modalWrap);
    fireEvent.click(modalWrap);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    onClose.mockClear();
    const name = screen.getByDisplayValue("旧名字");
    await userEvent.clear(name);
    await userEvent.type(name, "未保存");
    fireEvent.mouseDown(modalWrap);
    fireEvent.click(modalWrap);
    expect(document.querySelector(".ant-modal-confirm-title")).toHaveTextContent("退出用户设置？");
    await userEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(onClose).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("button", { name: "继续编辑" })).not.toBeInTheDocument());
    fireEvent.mouseDown(modalWrap);
    fireEvent.click(modalWrap);
    await userEvent.click(screen.getByRole("button", { name: /退/ }));
    expect(onClose).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(document.querySelector(".ant-modal-confirm-title")).toHaveTextContent("退出用户设置？");
  });

  it("saves the profile and updates the sidebar user immediately", async () => {
    const onUserUpdate = vi.fn();
    renderModal(vi.fn(), onUserUpdate);
    const name = await screen.findByDisplayValue("旧名字");
    await userEvent.clear(name);
    await userEvent.type(name, "新名字");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.updateProfile).toHaveBeenCalledWith({
      display_name: "新名字",
      agent_preferences: "",
    }));
    expect(onUserUpdate).toHaveBeenCalledWith({ display_name: "新名字", agent_preferences: "" });
    expect(screen.getByDisplayValue("新名字")).toBeInTheDocument();
  });
});
