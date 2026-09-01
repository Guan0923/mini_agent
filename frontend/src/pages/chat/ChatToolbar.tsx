import { BranchesOutlined, CommentOutlined, NodeIndexOutlined } from "@ant-design/icons";
import { Button, Dropdown, Tooltip } from "antd";
import AgentThreadPicker from "./AgentThreadPicker";

export type ChatMainView = "chat" | "trace";

interface AgentThreadPickerState {
  sessionId: string;
  rootThreadId: string;
  selectedThreadId: string;
  invalidation: number;
  onSelect: (threadId: string) => void;
}

interface ChatToolbarProps {
  visible: boolean;
  currentThreadId: string;
  compact: boolean;
  mainView: ChatMainView;
  agentThread?: AgentThreadPickerState;
  onMainViewChange: (view: ChatMainView) => void;
}

export function ChatToolbar({
  visible,
  currentThreadId,
  compact,
  mainView,
  agentThread,
  onMainViewChange,
}: ChatToolbarProps) {
  if (!visible) return null;
  return (
    <div className="trace-toolbar" role="navigation" aria-label="主内容视图">
      {agentThread ? (
        <AgentThreadPicker
          sessionId={agentThread.sessionId}
          rootThreadId={agentThread.rootThreadId}
          selectedThreadId={agentThread.selectedThreadId}
          compact={compact}
          invalidation={agentThread.invalidation}
          onSelect={agentThread.onSelect}
        />
      ) : (
        <Dropdown
          trigger={["click"]}
          menu={{
            selectable: true,
            selectedKeys: [currentThreadId],
            items: [{ key: currentThreadId, label: currentThreadId }],
          }}
        >
          <Tooltip title={compact ? `Thread：${currentThreadId}` : undefined}>
            <Button type="text" aria-label="Thread" icon={compact ? <BranchesOutlined /> : undefined}>
              {compact ? null : "Thread"}
            </Button>
          </Tooltip>
        </Dropdown>
      )}
      <span className="trace-toolbar-thread-id" title={currentThreadId}>{currentThreadId}</span>
      <Tooltip title={compact ? "Chat" : undefined}>
        <Button
          type="text"
          aria-label="Chat"
          icon={compact ? <CommentOutlined /> : undefined}
          aria-pressed={mainView === "chat"}
          onClick={() => onMainViewChange("chat")}
        >
          {compact ? null : "Chat"}
        </Button>
      </Tooltip>
      <Tooltip title={compact ? "Trace" : undefined}>
        <Button
          type="text"
          aria-label="Trace"
          icon={compact ? <NodeIndexOutlined /> : undefined}
          aria-pressed={mainView === "trace"}
          onClick={() => onMainViewChange("trace")}
        >
          {compact ? null : "Trace"}
        </Button>
      </Tooltip>
    </div>
  );
}
