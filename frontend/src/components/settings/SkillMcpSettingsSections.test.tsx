import { App as AntApp } from "antd";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { McpSettingsSection } from "./McpSettingsSection";
import { SkillSettingsSection } from "./SkillSettingsSection";

const api = vi.hoisted(() => ({
  getSkillSettings: vi.fn(),
  setSkillsEnabled: vi.fn(),
  setSkillEnabled: vi.fn(),
  importSkill: vi.fn(),
  deleteSkill: vi.fn(),
  getMcpSettings: vi.fn(),
  setMcpEnabled: vi.fn(),
  createMcpServer: vi.fn(),
  updateMcpServer: vi.fn(),
  setMcpServerEnabled: vi.fn(),
  deleteMcpServer: vi.fn(),
  testMcpServer: vi.fn(),
}));

vi.mock("../../api", () => api);

const skillSettings = {
  enabled: true,
  skills: [{
    directory: "demo-folder",
    name: "demo",
    description: "Demo workflow.",
    metadata: { owner: "local" },
    allowed_tools: ["read_file"],
    root: "C:/Users/demo/.mini_agent/skills/demo-folder",
    enabled: true,
  }],
};

const mcpServer = {
  name: "trace",
  command: "python",
  args: ["server.py"],
  cwd: null,
  env: { MODE: "test" },
  secret_env: [{ name: "API_TOKEN", configured: true }],
  enabled: true,
};

const mcpSettings = { enabled: false, servers: [mcpServer] };

function renderWithApp(node: React.ReactNode) {
  return render(<AntApp>{node}</AntApp>);
}

describe("SkillSettingsSection", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getSkillSettings.mockResolvedValue(structuredClone(skillSettings));
    api.setSkillsEnabled.mockResolvedValue(structuredClone(skillSettings));
    api.setSkillEnabled.mockResolvedValue(structuredClone(skillSettings));
    api.importSkill.mockResolvedValue({ directory: "imported" });
    api.deleteSkill.mockResolvedValue(undefined);
  });

  it("loads metadata on demand and rolls a failed row switch back", async () => {
    api.setSkillEnabled.mockRejectedValueOnce(new Error("保存失败"));
    renderWithApp(<SkillSettingsSection />);

    expect(await screen.findByText("Demo workflow.")).toBeInTheDocument();
    expect(screen.getByText("owner: local")).toBeInTheDocument();
    expect(screen.getByText("read_file")).toBeInTheDocument();
    const toggle = screen.getByRole("switch", { name: "启用 Skill demo" });
    expect(toggle).toBeChecked();

    await userEvent.click(toggle);
    await screen.findByText("保存失败");
    expect(toggle).toBeChecked();
  });

  it("refreshes after import and confirms permanent deletion", async () => {
    api.getSkillSettings
      .mockResolvedValueOnce(structuredClone(skillSettings))
      .mockResolvedValueOnce({ enabled: true, skills: [] })
      .mockResolvedValueOnce(structuredClone(skillSettings));
    renderWithApp(<SkillSettingsSection />);
    await screen.findByText("Demo workflow.");

    await userEvent.click(screen.getByRole("button", { name: /导入 Skill/ }));
    await waitFor(() => expect(api.importSkill).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.getSkillSettings).toHaveBeenCalledTimes(2));

    cleanup();
    renderWithApp(<SkillSettingsSection />);
    await screen.findByText("Demo workflow.");
    await userEvent.click(screen.getByRole("button", { name: "删除 Skill demo" }));
    await userEvent.click(await screen.findByRole("button", { name: "永久删除" }));
    await waitFor(() => expect(api.deleteSkill).toHaveBeenCalledWith("demo-folder"));
  });
});

describe("McpSettingsSection", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    api.getMcpSettings.mockResolvedValue(structuredClone(mcpSettings));
    api.setMcpEnabled.mockResolvedValue({ ...structuredClone(mcpSettings), enabled: true });
    api.setMcpServerEnabled.mockResolvedValue({ ...structuredClone(mcpServer), enabled: false });
    api.createMcpServer.mockResolvedValue(structuredClone(mcpServer));
    api.updateMcpServer.mockResolvedValue(structuredClone(mcpServer));
    api.deleteMcpServer.mockResolvedValue(undefined);
    api.testMcpServer.mockResolvedValue({ tools: ["mcp_trace_inspect_trace"], count: 1 });
  });

  it("shows redacted secret state and tests the saved connection", async () => {
    renderWithApp(<McpSettingsSection />);
    expect(await screen.findByText("trace")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "启用 MCP" })).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: /trace/ }));
    expect(await screen.findByText("已配置")).toBeInTheDocument();
    expect(screen.getByLabelText("MCP 密钥变量值 1")).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(api.testMcpServer).toHaveBeenCalledWith("trace"));
  });

  it("updates the explicit secret removal state immediately", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");
    await userEvent.click(screen.getByRole("button", { name: /trace/ }));

    const secretInput = await screen.findByLabelText("MCP 密钥变量值 1");
    await userEvent.click(screen.getByRole("button", { name: /清\s*除/ }));
    expect(await screen.findByText("待清除")).toBeInTheDocument();
    expect(secretInput).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: /撤\s*销\s*清\s*除/ }));
    expect(await screen.findByText("已配置")).toBeInTheDocument();
    expect(secretInput).not.toBeDisabled();
  });

  it("creates a structured server and immediately persists switches", async () => {
    renderWithApp(<McpSettingsSection />);
    await screen.findByText("trace");

    await userEvent.click(screen.getByRole("switch", { name: "启用 MCP" }));
    await waitFor(() => expect(api.setMcpEnabled).toHaveBeenCalledWith(true));
    await userEvent.click(screen.getByRole("switch", { name: "启用 MCP Server trace" }));
    await waitFor(() => expect(api.setMcpServerEnabled).toHaveBeenCalledWith("trace", false));

    await userEvent.click(screen.getByRole("button", { name: /新增 MCP Server/ }));
    await userEvent.type(screen.getByLabelText("MCP Server 名称"), "local");
    await userEvent.type(screen.getByLabelText("MCP Command new"), "python");
    await userEvent.click(screen.getByRole("button", { name: "创建 Server" }));
    await waitFor(() => expect(api.createMcpServer).toHaveBeenCalledWith(expect.objectContaining({
      name: "local",
      command: "python",
      args: [],
      env: {},
      secrets: {},
      enabled: true,
    })));
  });
});
