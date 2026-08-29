import { App as AntApp } from "antd";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import UserSettingsModal from "./UserSettingsModal";

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateProfile: vi.fn(),
  updateAgentConfig: vi.fn(),
  updateRuntimeConfig: vi.fn(),
  updateProviderConfig: vi.fn(),
  addProviderConfig: vi.fn(),
  updateProviderConfigById: vi.fn(),
  activateProviderConfig: vi.fn(),
  deleteProviderConfig: vi.fn(),
  discoverProviderModels: vi.fn(),
  setTimezone: vi.fn(),
  getSyncStatus: vi.fn(),
  getSyncJob: vi.fn(),
  updateSyncPreferences: vi.fn(),
  syncNow: vi.fn(),
}));

vi.mock("../api", () => api);

const settings = {
  profile: { email: "user@example.com", display_name: "旧名字", agent_preferences: "" },
  agent_config: { tone: "balanced", verbosity: "balanced", initiative: "balanced", custom_instructions: "" },
  runtime_config: { max_tool_calls: 32, terminal_type: "cmd" as const },
  sandbox_config: {
    enabled: true,
    file_mode: "read_only" as const,
    network_mode: "no_network" as const,
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
  },
  terminal_options: [
    { value: "cmd" as const, label: "命令提示符（cmd）" },
    { value: "powershell" as const, label: "Windows PowerShell" },
  ],
  terminal_notice: null,
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
    cloud_revision: 1,
    status: "dirty" as const,
    last_error: "",
    updated_at: 1,
  },
};

const authUser = { id: "u1", email: "user@example.com", kind: "account" as const, display_name: "user@example.com" };

function modalElement(
  open: boolean,
  onClose = vi.fn(),
  onUserUpdate = vi.fn(),
  onProviderConfigUpdate = vi.fn(),
) {
  return (
    <AntApp>
      <UserSettingsModal
        open={open}
        user={authUser}
        activeSessionId="session-current"
        onClose={onClose}
        onUserUpdate={onUserUpdate}
        onProviderConfigUpdate={onProviderConfigUpdate}
      />
    </AntApp>
  );
}

function renderModal(onClose = vi.fn(), onUserUpdate = vi.fn(), onProviderConfigUpdate = vi.fn()) {
  return render(modalElement(true, onClose, onUserUpdate, onProviderConfigUpdate));
}

describe("UserSettingsModal", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getSettings.mockResolvedValue(structuredClone(settings));
    api.updateProfile.mockResolvedValue({ display_name: "新名字", agent_preferences: "" });
    api.updateAgentConfig.mockResolvedValue(settings.agent_config);
    api.updateRuntimeConfig.mockResolvedValue(settings.runtime_config);
    api.updateProviderConfig.mockResolvedValue(settings.provider_config);
    api.discoverProviderModels.mockResolvedValue({ models: [] });
    api.getSyncStatus.mockResolvedValue({
      preferences: settings.sync_preferences,
      state: settings.sync_state,
      job: null,
    });
    api.updateSyncPreferences.mockImplementation(async (value) => value);
    api.syncNow.mockResolvedValue({
      id: "job-1",
      kind: "sync",
      status: "queued",
      phase: "queued",
      progress: 0,
      error: "",
      created_at: 1,
      updated_at: 1,
    });
  });

  it("renders the settings spinner inside the full content-area loading container", () => {
    api.getSettings.mockReturnValue(new Promise(() => undefined));
    renderModal();

    const loading = document.querySelector(".user-settings-modal .ant-modal-body > .user-settings-loading");
    expect(loading).toBeInTheDocument();
    expect(loading?.querySelector(".ant-spin")).toBeInTheDocument();
  });

  it("switches among profile, agent, and provider sections", async () => {
    renderModal();
    expect(await screen.findByDisplayValue("user@example.com")).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "用户名" })).toHaveValue("旧名字");

    await userEvent.click(screen.getByRole("menuitem", { name: "Agent 配置" }));
    expect(screen.getByRole("textbox", { name: "自由文本偏好" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "运行配置" }));
    expect(screen.getByRole("spinbutton", { name: "工具调用上限" })).toHaveValue("32");

    await userEvent.click(screen.getByRole("menuitem", { name: "添加提供商" }));
    expect(screen.getByText("Base URL")).toBeInTheDocument();
    expect(screen.getByText("Chat Completions")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));
    expect(screen.getByText(/openai/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "云同步" }));
    expect(screen.getByRole("switch", { name: "自动保存到云端" })).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "自动保存规则" })).toBeDisabled();
  });

  it("normalizes a legacy DeepSeek provider name to the neutral default", async () => {
    const legacyProvider = {
      ...settings.provider_config,
      provider: "deepseek",
      provider_name: "deepseek",
    };
    api.getSettings.mockResolvedValue({
      ...settings,
      provider_config: legacyProvider,
      provider_configs: [legacyProvider],
    });

    renderModal();
    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));

    expect(screen.getByText(/default · demo/)).toBeInTheDocument();
  });

  it("shows detected terminals and saves the selected Ant Design option", async () => {
    renderModal();
    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "运行配置" }));

    const terminal = screen.getByRole("combobox", { name: "启动终端" });
    fireEvent.mouseDown(terminal);
    expect(screen.getByRole("option", { name: "Windows PowerShell" })).toBeInTheDocument();
    fireEvent.click(screen.getByText("Windows PowerShell", { selector: ".ant-select-item-option-content" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.updateRuntimeConfig).toHaveBeenCalledWith({
      max_tool_calls: 32,
      terminal_type: "powershell",
    }));
  });

  it("does not expose sandbox controls in user settings", async () => {
    renderModal();
    await screen.findByDisplayValue("user@example.com");
    expect(screen.queryByRole("menuitem", { name: "沙箱" })).not.toBeInTheDocument();
    expect(screen.queryByText("Windows 沙箱")).not.toBeInTheDocument();
  });

  it("saves cloud preferences and starts an incremental event sync", async () => {
    renderModal();
    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "云同步" }));
    await userEvent.click(screen.getByRole("switch", { name: "自动保存到云端" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.updateSyncPreferences).toHaveBeenCalledWith({
      auto_save_enabled: true,
      auto_save_rule: "idle_5m",
    }));
    expect((await screen.findAllByText("保存成功")).length).toBeGreaterThanOrEqual(1);
    await userEvent.click(screen.getByRole("button", { name: "立即同步" }));
    await waitFor(() => expect(api.syncNow).toHaveBeenCalledWith(false));
    expect(await screen.findByText("同步已启动")).toBeInTheDocument();
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
    expect(await screen.findByText("保存成功")).toBeInTheDocument();
  });

  it("does not show a success message when saving fails", async () => {
    api.updateProfile.mockRejectedValueOnce(new Error("保存失败"));
    renderModal();
    const name = await screen.findByDisplayValue("旧名字");
    await userEvent.clear(name);
    await userEvent.type(name, "新名字");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByText("保存失败")).toBeInTheDocument();
    expect(screen.queryByText("保存成功")).not.toBeInTheDocument();
  });

  it("switches the current Provider and model from user settings", async () => {
    const nextProvider = {
      ...settings.provider_config,
      id: "provider-2",
      is_active: false,
      provider: "anthropic",
      provider_name: "anthropic",
      protocol: "messages" as const,
      model: "claude-settings",
    };
    api.getSettings.mockResolvedValue({
      ...structuredClone(settings),
      provider_configs: [structuredClone(settings.provider_config), nextProvider],
    });
    api.activateProviderConfig.mockResolvedValue({ ...nextProvider, is_active: true });
    const onProviderConfigUpdate = vi.fn();
    renderModal(vi.fn(), vi.fn(), onProviderConfigUpdate);

    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));
    await userEvent.click(screen.getByText(/anthropic · claude-settings/));
    await userEvent.click(screen.getByRole("button", { name: "设为当前使用" }));

    await waitFor(() => expect(api.activateProviderConfig).toHaveBeenCalledWith("provider-2"));
    expect(onProviderConfigUpdate).toHaveBeenCalledWith(expect.objectContaining({
      provider_name: "anthropic",
      model: "claude-settings",
      is_active: true,
    }));
  });

  it("opens every discovered model before filtering and keeps selection editable", async () => {
    api.discoverProviderModels.mockResolvedValueOnce({
      models: ["alpha-model", "beta-model"],
    });
    renderModal();

    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));
    await userEvent.click(screen.getByText(/openai · demo/));
    await userEvent.click(screen.getByRole("button", { name: "获取 /v1\/models" }));

    await waitFor(() => expect(api.discoverProviderModels).toHaveBeenCalledWith({
      config_id: "provider-1",
      provider_name: "openai",
      protocol: "chat_completions",
      base_url: "https://example.test/v1",
      api_key: "",
    }));
    expect(await screen.findByText("已获取 2 个模型")).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: "alpha-model" })).toBeInTheDocument();

    const modelInput = screen.getByDisplayValue("demo");
    await userEvent.clear(modelInput);
    await userEvent.type(modelInput, "beta");
    expect(screen.queryByRole("option", { name: "alpha-model" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("beta-model", { selector: ".ant-select-item-option-content" }));
    await waitFor(() => expect(modelInput).toHaveValue("beta-model"));
    await waitFor(() => expect(screen.queryByRole("option", { name: "beta-model" })).not.toBeInTheDocument());
  });

  it("shows empty and failed discovery results on their own Providers", async () => {
    const nextProvider = {
      ...settings.provider_config,
      id: "provider-2",
      is_active: false,
      provider: "anthropic",
      provider_name: "anthropic",
      protocol: "messages" as const,
      model: "claude-settings",
    };
    api.getSettings.mockResolvedValue({
      ...structuredClone(settings),
      provider_configs: [structuredClone(settings.provider_config), nextProvider],
    });
    api.discoverProviderModels
      .mockResolvedValueOnce({ models: [] })
      .mockRejectedValueOnce(new Error("密钥无效"));
    renderModal();

    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));
    await userEvent.click(screen.getByText(/openai · demo/));
    await userEvent.click(screen.getByText(/anthropic · claude-settings/));
    const discoverButtons = screen.getAllByRole("button", { name: "获取 /v1\/models" });
    await userEvent.click(discoverButtons[0]);
    expect(await screen.findByText("模型服务没有返回可用模型，请继续手动输入。")).toBeInTheDocument();
    await userEvent.click(discoverButtons[1]);

    expect(await screen.findByText("密钥无效")).toBeInTheDocument();
    expect(screen.getByText("模型服务没有返回可用模型，请继续手动输入。")).toBeInTheDocument();
  });

  it("clears managed model feedback when settings close", async () => {
    api.discoverProviderModels.mockResolvedValueOnce({ models: ["alpha-model"] });
    const view = renderModal();

    await screen.findByDisplayValue("user@example.com");
    await userEvent.click(screen.getByRole("menuitem", { name: "Provider 与模型" }));
    await userEvent.click(screen.getByText(/openai · demo/));
    await userEvent.click(screen.getByRole("button", { name: "获取 /v1\/models" }));
    expect(await screen.findByText("已获取 1 个模型")).toBeInTheDocument();

    view.rerender(modalElement(false));
    view.rerender(modalElement(true));
    await screen.findByRole("heading", { name: "Provider 与模型" });
    await waitFor(() => expect(screen.queryByText("已获取 1 个模型")).not.toBeInTheDocument());
  });
});
