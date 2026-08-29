import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MemoryPage from "./MemoryPage";

const mocks = vi.hoisted(() => ({
  cancelMemoryJob: vi.fn(),
  clearMemories: vi.fn(),
  consolidateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  extractMemory: vi.fn(),
  getSettings: vi.fn(),
  listMemoryEvidence: vi.fn(),
  listMemoryInjectionHistory: vi.fn(),
  listMemoryItems: vi.fn(),
  listMemoryJobs: vi.fn(),
  listSessions: vi.fn(),
  restoreMemory: vi.fn(),
  setMemoryEnabled: vi.fn(),
  updateMemoryConfig: vi.fn(),
}));

vi.mock("../api", () => mocks);

const config = {
  use_memories: true,
  generate_memories: false,
  automatic_memory_enabled: false,
  disable_on_external_context: true,
  extraction_model: "",
  consolidation_model: "",
  retrieval_limit: 40,
  injection_max_items: 8,
  injection_max_tokens: 1200,
  injection_max_bytes: 8192,
};

const item = {
  memory_id: "memory_a",
  kind: "semantic",
  title: "Concise reports",
  content: "The user prefers concise technical reports.",
  summary: "Concise",
  scope: "global",
  project_id: null,
  confidence: 0.9,
  tags: ["preference"],
  status: "active",
  created_at: "2026-01-01T00:00:00+00:00",
  updated_at: "2026-01-01T00:00:00+00:00",
  deleted_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSettings.mockResolvedValue({ memory_config: config });
  mocks.listMemoryItems.mockResolvedValue([item]);
  mocks.listMemoryJobs.mockResolvedValue([]);
  mocks.listSessions.mockResolvedValue([{ session_id: "session_a", title: "Session A" }]);
  mocks.listMemoryInjectionHistory.mockResolvedValue([]);
  mocks.listMemoryEvidence.mockResolvedValue([]);
  mocks.updateMemoryConfig.mockImplementation(async (value) => value);
  mocks.setMemoryEnabled.mockResolvedValue({ ...item, status: "disabled" });
  mocks.clearMemories.mockResolvedValue(undefined);
});

describe("MemoryPage management", () => {
  afterEach(() => cleanup());

  it("loads memory state and updates the generation switch", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    expect(await screen.findByText("Concise reports")).toBeInTheDocument();
    const switches = screen.getAllByRole("switch");
    expect(switches[2]).toBeDisabled();
    await user.click(switches[0]);
    await waitFor(() => expect(mocks.updateMemoryConfig).toHaveBeenCalledWith({
      ...config,
      generate_memories: true,
      automatic_memory_enabled: false,
    }));
    expect(switches[2]).toBeEnabled();
  });

  it("requires the exact confirmation before clearing", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);
    await screen.findByText("Concise reports");

    await user.click(screen.getByRole("button", { name: /清空全部 Memory/ }));
    const confirm = screen.getByRole("button", { name: "永久清空" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "CLEAR ALL MEMORIES");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(mocks.clearMemories).toHaveBeenCalledWith("CLEAR ALL MEMORIES"));
  });

  it("renders project-scoped memory returned by the management API", async () => {
    mocks.listMemoryItems.mockResolvedValue([
      {
        ...item,
        memory_id: "memory_project",
        title: "Project preference",
        scope: "project",
        project_id: "project_a",
      },
    ]);

    render(<MemoryPage />);

    expect(await screen.findByText("Project preference")).toBeInTheDocument();
    expect(screen.getByText("项目 project_a")).toBeInTheDocument();
  });
});
