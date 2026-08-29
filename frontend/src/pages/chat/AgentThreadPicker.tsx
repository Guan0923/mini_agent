import { BranchesOutlined } from "@ant-design/icons";
import { Button, Popover, Tree, Tooltip } from "antd";
import { useEffect, useState, type Key, type ReactNode } from "react";
import { listAgentThreadChildren } from "../../api";
import type { AgentThreadSummary } from "../../types";

interface PickerNode {
  key: string;
  title: ReactNode;
  isLeaf?: boolean;
  children?: PickerNode[];
}

interface AgentThreadPickerProps {
  sessionId: string;
  rootThreadId: string;
  selectedThreadId: string;
  compact: boolean;
  invalidation: number;
  onSelect: (threadId: string) => void;
}

function nodeTitle(summary: AgentThreadSummary): string {
  const parts = summary.thread_path.split("/").filter(Boolean);
  const name = parts[parts.length - 1] ?? summary.thread_id;
  return `${name} · ${summary.thread_status}`;
}

function updateNode(nodes: PickerNode[], key: Key, children: PickerNode[]): PickerNode[] {
  return nodes.map((node) => {
    if (node.key === key) return { ...node, children, isLeaf: children.length === 0 };
    return node.children ? { ...node, children: updateNode(node.children, key, children) } : node;
  });
}

export default function AgentThreadPicker({
  sessionId,
  rootThreadId,
  selectedThreadId,
  compact,
  invalidation,
  onSelect,
}: AgentThreadPickerProps) {
  const [open, setOpen] = useState(false);
  const [treeData, setTreeData] = useState<PickerNode[]>([
    { key: rootThreadId, title: "root", isLeaf: false },
  ]);

  useEffect(() => {
    setTreeData([{ key: rootThreadId, title: "root", isLeaf: false }]);
  }, [sessionId, rootThreadId, invalidation]);

  async function loadChildren(node: PickerNode): Promise<void> {
    if (node.children) return;
    const children = await listAgentThreadChildren(sessionId, String(node.key));
    setTreeData((current) => updateNode(
      current,
      node.key,
      children.map((item) => ({ key: item.thread_id, title: nodeTitle(item), isLeaf: false })),
    ));
  }

  const picker = (
    <Tree<PickerNode>
      aria-label="Agent Thread 树"
      blockNode
      loadData={loadChildren}
      selectedKeys={[selectedThreadId]}
      treeData={treeData}
      onSelect={(keys) => {
        const selected = keys[0];
        if (selected == null) return;
        onSelect(String(selected));
        setOpen(false);
      }}
    />
  );

  return (
    <Popover
      content={picker}
      open={open}
      placement="bottomLeft"
      trigger="click"
      onOpenChange={setOpen}
    >
      <Tooltip title={compact ? `Thread：${selectedThreadId}` : undefined}>
        <Button type="text" aria-label="Thread" icon={compact ? <BranchesOutlined /> : undefined}>
          {compact ? null : "Thread"}
        </Button>
      </Tooltip>
    </Popover>
  );
}
