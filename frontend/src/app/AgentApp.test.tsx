import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionInfo } from "../api";
import type { ProjectInfo } from "../api/projects";
import type { AuthUser } from "../types";
import type { AgentShellProps } from "./AgentShell";
import AgentApp from "./AgentApp";

const api = vi.hoisted(() => ({
  archiveSession: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  forkTurn: vi.fn(),
  getSettings: vi.fn(),
  getSessionNodes: vi.fn(),
  listSessions: vi.fn(),
  renameSession: vi.fn(),
  restoreSession: vi.fn(),
  updateProfile: vi.fn(),
}));

const projectsApi = vi.hoisted(() => ({
  changeProjectPath: vi.fn(),
  createProject: vi.fn(),
  createProjectSession: vi.fn(),
  listProjects: vi.fn(),
  removeProject: vi.fn(),
  renameProject: vi.fn(),
  restoreProject: vi.fn(),
  revokeProjectSkillTrust: vi.fn(),
}));

const auth = vi.hoisted(() => ({
  user: {
    id: "user-1",
    email: "user@example.com",
    kind: "account",
    display_name: "User",
  } as AuthUser,
  setUser: vi.fn(),
  signOut: vi.fn(),
}));

const shell = vi.hoisted(() => ({ props: null as AgentShellProps | null }));

vi.mock("../api", () => api);
vi.mock("../api/projects", () => projectsApi);
vi.mock("../auth/AuthProvider", () => ({ useAuth: () => auth }));
vi.mock("./AgentShell", () => ({
  default: (props: AgentShellProps) => {
    shell.props = props;
    return <div data-testid="agent-shell" />;
  },
}));

function session(sessionId: string, projectId?: string): SessionInfo {
  return {
    session_id: sessionId,
    title: "新对话",
    created_at: "2026-08-21T00:00:00Z",
    updated_at: "2026-08-21T00:00:00Z",
    message_count: 0,
    last_run_status: null,
    client_id: `${sessionId}-client`,
    project_id: projectId,
  };
}

function project(projectId: string, sessionIds: string[] = []): ProjectInfo {
  return {
    project_id: projectId,
    name: "测试项目",
    cwd: "C:\\workspace",
    available: true,
    created_at: "2026-08-21T00:00:00Z",
    updated_at: "2026-08-21T00:00:00Z",
    conversation_count: sessionIds.length,
    session_ids: sessionIds,
  };
}

async function renderReady(): Promise<void> {
  render(<AgentApp />);
  await waitFor(() => expect(shell.props?.projectsLoaded).toBe(true));
}

async function expectKnownEmptySession(sessionId: string): Promise<void> {
  await waitFor(() => expect(shell.props?.current?.sessionId).toBe(sessionId));
  expect(shell.props?.current).toMatchObject({
    messages: [],
    messagesLoaded: true,
    runtimeNodes: [],
  });
  await act(async () => Promise.resolve());
  expect(api.getSessionNodes).not.toHaveBeenCalled();
}

describe("AgentApp new conversation initialization", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    shell.props = null;
    api.getSettings.mockRejectedValue(new Error("settings unavailable"));
    api.getSessionNodes.mockResolvedValue([]);
    api.listSessions.mockResolvedValue([]);
    projectsApi.listProjects.mockResolvedValue([]);
  });

  it("marks an ordinary new conversation as having a known empty runtime tree", async () => {
    api.createSession.mockResolvedValue(session("session-ordinary"));
    await renderReady();

    await act(async () => {
      await shell.props!.onNew();
    });

    await expectKnownEmptySession("session-ordinary");
  });

  it("marks the initial conversation of a new project as having a known empty runtime tree", async () => {
    const projectInfo = project("project-new", ["session-project"]);
    projectsApi.createProject.mockResolvedValue({
      project: projectInfo,
      session: session("session-project", projectInfo.project_id),
    });
    await renderReady();

    await act(async () => {
      await shell.props!.onNewProject();
    });

    await expectKnownEmptySession("session-project");
  });

  it("marks a new project conversation as having a known empty runtime tree", async () => {
    const projectInfo = project("project-existing", ["session-project-new"]);
    projectsApi.listProjects.mockImplementation(async (state: "active" | "removed") => (
      state === "active" ? [projectInfo] : []
    ));
    projectsApi.createProjectSession.mockResolvedValue({
      project: projectInfo,
      session: session("session-project-new", projectInfo.project_id),
    });
    await renderReady();

    await act(async () => {
      await shell.props!.onNewProjectConversation(projectInfo.project_id);
    });

    await expectKnownEmptySession("session-project-new");
  });
});
