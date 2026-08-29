import { App as AntApp } from "antd";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AgentThreadPicker from "./AgentThreadPicker";

const api = vi.hoisted(() => ({ listAgentThreadChildren: vi.fn() }));
vi.mock("../../api", () => api);

describe("AgentThreadPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listAgentThreadChildren.mockImplementation(async (_sessionId: string, threadId: string) => {
      if (threadId === "session_1") {
        return [{
          thread_id: "thread_child",
          thread_path: "/root/worker",
          thread_task: "direct task",
          thread_status: "opening",
        }];
      }
      if (threadId === "thread_child") {
        return [{
          thread_id: "thread_grandchild",
          thread_path: "/root/worker/nested",
          thread_task: "nested task",
          thread_status: "opening",
        }];
      }
      return [];
    });
  });

  it("loads only direct children for each expanded level and selects a grandchild", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <AntApp>
        <AgentThreadPicker
          sessionId="session_1"
          rootThreadId="session_1"
          selectedThreadId="session_1"
          compact={false}
          invalidation={0}
          onSelect={onSelect}
        />
      </AntApp>,
    );
    await user.click(screen.getByRole("button", { name: "Thread" }));
    const tree = await screen.findByRole("tree", { name: "Agent Thread 树" });
    const root = within(tree).getByText("root").closest('[role="treeitem"]')!;
    fireEvent.click(root.querySelector(".ant-tree-switcher")!);

    await waitFor(() => expect(api.listAgentThreadChildren).toHaveBeenCalledWith("session_1", "session_1"));
    expect(api.listAgentThreadChildren).toHaveBeenCalledTimes(1);
    const child = (await within(tree).findByText("worker · opening")).closest('[role="treeitem"]')!;
    fireEvent.click(child.querySelector(".ant-tree-switcher")!);

    await waitFor(() => expect(api.listAgentThreadChildren).toHaveBeenCalledWith("session_1", "thread_child"));
    expect(api.listAgentThreadChildren).toHaveBeenCalledTimes(2);
    await user.click(await within(tree).findByText("nested · opening"));
    expect(onSelect).toHaveBeenCalledWith("thread_grandchild");
  });

  it("drops the previous lazy cache when the Sidebar root changes inside one Session", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <AntApp>
        <AgentThreadPicker
          sessionId="session_1"
          rootThreadId="session_1"
          selectedThreadId="session_1"
          compact={false}
          invalidation={0}
          onSelect={vi.fn()}
        />
      </AntApp>,
    );
    await user.click(screen.getByRole("button", { name: "Thread" }));
    let tree = await screen.findByRole("tree", { name: "Agent Thread 树" });
    fireEvent.click(within(tree).getByText("root").closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher")!);
    expect(await within(tree).findByText("worker · opening")).toBeInTheDocument();

    rerender(
      <AntApp>
        <AgentThreadPicker
          sessionId="session_1"
          rootThreadId="thread_fork"
          selectedThreadId="thread_fork"
          compact={false}
          invalidation={0}
          onSelect={vi.fn()}
        />
      </AntApp>,
    );
    const previousTree = tree;
    await waitFor(() => expect(screen.getByRole("tree", { name: "Agent Thread 树" })).not.toBe(previousTree));
    tree = screen.getByRole("tree", { name: "Agent Thread 树" });
    await waitFor(() => expect(within(tree).queryByText("worker · opening")).not.toBeInTheDocument());
    fireEvent.click(within(tree).getByText("root").closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher")!);
    await waitFor(() => expect(api.listAgentThreadChildren).toHaveBeenCalledWith("session_1", "thread_fork"));
  });

  it("reloads a previously empty root after its invalidation changes", async () => {
    const user = userEvent.setup();
    api.listAgentThreadChildren
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([{
        thread_id: "thread_child",
        thread_path: "/root/worker",
        thread_task: "direct task",
        thread_status: "opening",
      }]);
    const { rerender } = render(
      <AntApp>
        <AgentThreadPicker
          sessionId="session_1"
          rootThreadId="session_1"
          selectedThreadId="session_1"
          compact={false}
          invalidation={0}
          onSelect={vi.fn()}
        />
      </AntApp>,
    );
    await user.click(screen.getByRole("button", { name: "Thread" }));
    let tree = await screen.findByRole("tree", { name: "Agent Thread 树" });
    fireEvent.click(within(tree).getByText("root").closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher")!);
    await waitFor(() => expect(api.listAgentThreadChildren).toHaveBeenCalledTimes(1));
    expect(within(tree).queryByText("worker · opening")).not.toBeInTheDocument();

    rerender(
      <AntApp>
        <AgentThreadPicker
          sessionId="session_1"
          rootThreadId="session_1"
          selectedThreadId="session_1"
          compact={false}
          invalidation={1}
          onSelect={vi.fn()}
        />
      </AntApp>,
    );
    tree = await screen.findByRole("tree", { name: "Agent Thread 树" });
    await waitFor(() => expect(
      within(tree).getByText("root").closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher"),
    ).not.toBeNull());
    fireEvent.click(within(tree).getByText("root").closest('[role="treeitem"]')!.querySelector(".ant-tree-switcher")!);

    await waitFor(() => expect(api.listAgentThreadChildren).toHaveBeenCalledTimes(2));
    expect(await within(tree).findByText("worker · opening")).toBeInTheDocument();
  });
});
