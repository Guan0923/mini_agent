import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionInfo } from "../api";
import type { ProjectInfo } from "../api/projects";
import type { AuthUser, RuntimeStateNode } from "../types";
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
  pauseTurn: vi.fn(),
  streamAttachedTurn: vi.fn(),
  streamChat: vi.fn(),
  streamResume: vi.fn(),
  streamRewind: vi.fn(),
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

function turn(
  sessionId: string,
  threadId: string,
  turnId: string,
  parent?: RuntimeStateNode,
  userText = "源消息",
): RuntimeStateNode {
  return {
    thread_id: threadId,
    parent_thread_id: parent?.thread_id ?? "",
    session_id: sessionId,
    parent_session_id: parent?.session_id ?? "",
    id: turnId,
    parent_id: parent?.id ?? "",
    version: "0.0.1",
    firstKeptItemSize: 8,
    compactionId: turnId,
    user: "user-1",
    provider_name: "local",
    model: {
      reasoning_effort: "medium",
      current_model: "test",
      context_length: 4096,
      output_length: 512,
      thinking: "enable",
      temperature: 0,
    },
    permission_mode: "read_only",
    running_mode: "agent",
    usage: { input_tokens: 1, cached_tokens: 0, output_tokens: 1, reasoning_tokens: 0, total_tokens: 2 },
    cwd: "C:\\workspace",
    timestamp: "2026-08-26T00:00:00Z",
    status: "success",
    current_data_idx: 0,
    data: [[
      { role: "user", content: [{ type: "text", text: userText }] },
      { role: "assistant", content: [{ type: "text", text: "回答" }] },
    ]],
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
    api.streamAttachedTurn.mockResolvedValue("completed");
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

  it("keeps the authoritative first-message title after the optimistic title is refreshed", async () => {
    const initial = session("session-title");
    initial.thread_id = initial.session_id;
    api.listSessions.mockResolvedValue([initial]);
    await renderReady();
    await waitFor(() => expect(shell.props?.current?.id).toBe(initial.session_id));

    act(() => {
      shell.props!.onUpdate(initial.session_id, (current) => ({ ...current, title: "临时乐观标题" }));
    });
    await waitFor(() => expect(shell.props?.current?.title).toBe("临时乐观标题"));

    api.listSessions.mockResolvedValue([{ ...initial, title: "第一条用户消息", title_is_custom: false }]);
    await act(async () => {
      await shell.props!.onRefresh();
    });

    expect(shell.props?.current?.title).toBe("第一条用户消息");
  });

  it("keeps the active rewind boundary when sidebar summaries and the full Turn tree reload", async () => {
    const initial = { ...session("session-rewind"), thread_id: "session-rewind" };
    const root = turn(initial.session_id, initial.thread_id, "turn-root", undefined, "保留消息");
    const descendant = turn(initial.session_id, initial.thread_id, "turn-descendant", root, "应隐藏消息");
    api.listSessions.mockResolvedValue([initial]);
    api.getSessionNodes.mockResolvedValue([root, descendant]);
    await renderReady();
    await waitFor(() => expect(shell.props?.current?.activeTurnId).toBe(descendant.id));

    act(() => {
      shell.props!.onUpdate(initial.thread_id, (current) => ({
        ...current,
        activeTurnId: root.id,
        lastNodeId: root.id,
      }));
    });
    await act(async () => {
      await shell.props!.onRefresh();
      await shell.props!.onReload(initial.thread_id);
    });

    expect(shell.props?.current?.activeTurnId).toBe(root.id);
    expect(shell.props?.current?.runtimeNodes).toHaveLength(2);
    expect(shell.props?.current?.messages.map((message) => message.content)).toEqual(["保留消息", "回答"]);
  });

  it("lets the backend inherit the source title when the UI creates a fork", async () => {
    const source = { ...session("session-fork"), thread_id: "session-fork", title: "源对话标题" };
    api.listSessions.mockResolvedValue([source]);
    await renderReady();
    await waitFor(() => expect(shell.props?.current?.id).toBe(source.thread_id));
    act(() => {
      shell.props!.onUpdate(source.thread_id, (current) => ({
        ...current,
        messagesLoaded: true,
        messages: [{
          id: "assistant-source",
          role: "assistant",
          content: "回答",
          events: [],
          sourceNodeId: "turn-source",
        }],
      }));
    });

    const forkedTurn = turn(source.session_id, "thread-fork", "turn-fork");
    api.forkTurn.mockResolvedValue({
      turn: forkedTurn,
      sidebar_thread: {
        thread_id: "thread-fork",
        session_id: source.session_id,
        title: source.title,
        created_at: "2026-08-26T00:00:00Z",
        updated_at: "2026-08-26T00:00:00Z",
        title_is_custom: false,
      },
    });
    api.getSessionNodes.mockResolvedValue([forkedTurn]);

    await act(async () => {
      await shell.props!.onFork(source.thread_id, "assistant-source");
    });

    expect(api.forkTurn).toHaveBeenCalledWith("turn-source");
    expect(shell.props?.current).toMatchObject({ id: "thread-fork", title: "源对话标题" });
  });

  it("attaches exactly once to a running Turn loaded after refresh", async () => {
    const summary = { ...session("session-running"), thread_id: "session-running" };
    const running = turn("session-running", "session-running", "turn-running");
    running.status = "running";
    running.data[0][1].content = [{ type: "reasoning", text: "已恢复" }];
    api.listSessions.mockResolvedValue([summary]);
    api.getSessionNodes.mockResolvedValue([running]);
    let finish!: () => void;
    api.streamAttachedTurn.mockImplementation(async (_turnId, onMessage) => {
      onMessage({ type: "turn.snapshot", revision: 0, turn: running });
      await new Promise<void>((resolve) => { finish = resolve; });
      return "completed";
    });

    await renderReady();

    await waitFor(() => expect(api.streamAttachedTurn).toHaveBeenCalledTimes(1));
    expect(api.streamAttachedTurn).toHaveBeenCalledWith(
      "turn-running",
      expect.any(Function),
      expect.any(AbortSignal),
    );
    await act(async () => Promise.resolve());
    expect(api.streamAttachedTurn).toHaveBeenCalledTimes(1);
    await act(async () => finish());
  });
});
