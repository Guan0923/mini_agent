import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import UserSettingsModal from "./UserSettingsModal";

const api = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateProfile: vi.fn(),
  updateAgentConfig: vi.fn(),
  updateProviderConfig: vi.fn(),
}));

vi.mock("../api", () => api);

const settings = {
  profile: { email: "user@example.com", display_name: "旧名字", agent_preferences: "" },
  agent_config: { tone: "balanced", verbosity: "balanced", initiative: "balanced", custom_instructions: "" },
  provider_config: {
    provider: "openai",
    protocol: "chat_completions" as const,
    base_url: "https://example.test/v1",
    model: "demo",
    max_tokens: 8192,
    context_size: 1024000,
    tokenizer_model: "demo",
    api_key_configured: false,
  },
  capability_config: {},
};

function renderModal(onClose = vi.fn(), onUserUpdate = vi.fn()) {
  return render(
    <AntApp>
      <UserSettingsModal
        open
        user={{ id: "u1", email: "user@example.com", legacy_owner: false }}
        onClose={onClose}
        onUserUpdate={onUserUpdate}
      />
    </AntApp>,
  );
}

describe("UserSettingsModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getSettings.mockResolvedValue(structuredClone(settings));
    api.updateProfile.mockResolvedValue({ display_name: "新名字", agent_preferences: "" });
    api.updateAgentConfig.mockResolvedValue(settings.agent_config);
    api.updateProviderConfig.mockResolvedValue(settings.provider_config);
  });

  it("switches among profile, agent, and provider sections", async () => {
    renderModal();
    expect(await screen.findByDisplayValue("user@example.com")).toBeDisabled();

    await userEvent.click(screen.getByRole("menuitem", { name: "Agent 配置" }));
    expect(screen.getByRole("textbox", { name: "自由文本偏好" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("menuitem", { name: "提供商" }));
    expect(screen.getByText("Base URL")).toBeInTheDocument();
    expect(screen.getByText("Chat Completions")).toBeInTheDocument();
  });

  it("only closes through the top-right close button", async () => {
    const onClose = vi.fn();
    renderModal(onClose);
    await screen.findByDisplayValue("旧名字");

    const mask = document.querySelector(".ant-modal-mask");
    if (!mask) throw new Error("modal mask not rendered");
    fireEvent.click(mask);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
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
