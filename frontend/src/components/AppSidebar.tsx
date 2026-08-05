import {
  Badge,
  App as AntApp,
  Button,
  Divider,
  Dropdown,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Space,
  Spin,
  Typography,
  type MenuProps,
} from "antd";
import {
  DeleteOutlined,
  EditOutlined,
  InboxOutlined,
  LoadingOutlined,
  LogoutOutlined,
  MoreOutlined,
  PlusOutlined,
  BarChartOutlined,
  MessageOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import type { AuthUser, Conversation, Page } from "../types";
import IconAction from "./IconAction";

interface AppSidebarProps {
  user: AuthUser | null;
  conversations: Conversation[];
  archivedCount: number;
  currentId: string | null;
  page: Page;
  onNew: () => void | Promise<unknown>;
  onSelect: (id: string) => void;
  onNavigate: (page: Page) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
  onSignOut: () => void | Promise<void>;
}

function confirmDelete(
  title: string,
  onDelete: () => void | Promise<void>,
  confirm: typeof Modal.confirm = Modal.confirm,
) {
  confirm({
    title: `删除“${title || "新对话"}”？`,
    content: "删除后将从界面隐藏，但后台仍保留审计数据。确定继续吗？",
    okText: "删除",
    cancelText: "取消",
    okButtonProps: { danger: true },
    onOk: onDelete,
  });
}

interface HistoryActionsProps {
  conversation: Conversation;
  onRename: (id: string, title: string) => Promise<void>;
  onArchive: (id: string) => Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
}

function HistoryActions({ conversation, onRename, onArchive, onDelete }: HistoryActionsProps) {
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameSaving, setRenameSaving] = useState(false);
  const [form] = Form.useForm<{ title: string }>();
  const { modal } = AntApp.useApp();
  const confirmModal = modal && typeof modal.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;
  const title = conversation.title || "新对话";
  const busy = conversation.messages.some((message) => message.running);

  function openRename() {
    form.setFieldsValue({ title: conversation.title || "" });
    setRenameOpen(true);
  }

  function closeRename() {
    if (renameSaving) return;
    setRenameOpen(false);
    form.resetFields();
  }

  async function submitRename(values: { title: string }) {
    const next = values.title.trim();
    if (!next) {
      form.setFields([{ name: "title", errors: ["请输入会话标题"] }]);
      return;
    }
    setRenameSaving(true);
    try {
      await onRename(conversation.id, next);
      setRenameOpen(false);
      form.resetFields();
    } catch {
      // The parent renders the mutation error; leave the editor open for correction.
    } finally {
      setRenameSaving(false);
    }
  }

  const menu: MenuProps = {
    items: [
      { key: "rename", label: "重命名", icon: <EditOutlined /> },
      { type: "divider" },
      { key: "archive", label: "归档", icon: <InboxOutlined />, disabled: busy },
      { key: "delete", label: "删除", icon: <DeleteOutlined />, danger: true, disabled: busy },
    ],
    onClick: ({ key }) => {
      if (key === "rename") {
        openRename();
      } else if (key === "archive") {
        void onArchive(conversation.id);
      } else if (key === "delete") {
        confirmDelete(title, () => onDelete(conversation.id), confirmModal);
      }
    },
  };

  return (
    <>
      <Dropdown menu={menu} trigger={["click"]} placement="bottomRight">
        <IconAction
          label={`更多操作：${title}`}
          icon={<MoreOutlined />}
          aria-haspopup="menu"
        />
      </Dropdown>
      <Modal
        title="重命名"
        open={renameOpen}
        onCancel={closeRename}
        okText="保存"
        cancelText="取消"
        confirmLoading={renameSaving}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={(values) => void submitRename(values)}>
          <Form.Item
            name="title"
            label="新标题"
            rules={[{ required: true, whitespace: true, message: "请输入会话标题" }]}
          >
            <Input aria-label="新标题" autoFocus maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

export default function AppSidebar({
  user,
  conversations,
  archivedCount,
  currentId,
  page,
  onNew,
  onSelect,
  onNavigate,
  onRename,
  onArchive,
  onDelete,
  onSignOut,
}: AppSidebarProps) {
  return (
    <div
      className="app-sidebar"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        minHeight: 0,
        padding: 16,
        background: "#fff",
      }}
    >
      <Button
        type="primary"
        size="large"
        block
        icon={<PlusOutlined />}
        onClick={() => void onNew()}
        aria-label="新建对话"
      >
        新建对话
      </Button>

      <Typography.Text type="secondary" style={{ margin: "20px 8px 8px", fontSize: 12 }}>
        对话
      </Typography.Text>
      <div style={{ minHeight: 0, flex: 1, overflowY: "auto" }}>
        <List
          size="small"
          split={false}
          dataSource={conversations}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" /> }}
          renderItem={(conversation) => {
            const running = conversation.messages.some((message) => message.running);
            const selected = conversation.id === currentId && page === "chat";
            const title = conversation.title || "新对话";
            return (
              <List.Item
                key={conversation.id}
                style={{ padding: "2px 0", border: 0 }}
                actions={[
                  <HistoryActions
                    key={`${conversation.id}-actions`}
                    conversation={conversation}
                    onRename={onRename}
                    onArchive={onArchive}
                    onDelete={onDelete}
                  />,
                ]}
              >
                <Button
                  type="text"
                  block
                  title={title}
                  onClick={() => onSelect(conversation.id)}
                  aria-label={title}
                  aria-current={selected ? "page" : undefined}
                  style={{
                    justifyContent: "flex-start",
                    minWidth: 0,
                    overflow: "hidden",
                    textAlign: "left",
                    background: selected ? "rgba(8, 127, 141, 0.1)" : undefined,
                  }}
                >
                  <Space size={7} style={{ minWidth: 0, maxWidth: "100%" }}>
                    {running ? (
                      <span role="status" aria-label="正在运行" title="正在运行">
                        <Badge status="processing" />
                        <Spin
                          size="small"
                          indicator={<LoadingOutlined spin />}
                          style={{ marginLeft: 2 }}
                        />
                      </span>
                    ) : (
                      <MessageOutlined aria-hidden="true" />
                    )}
                    <Typography.Text ellipsis style={{ maxWidth: "100%" }}>
                      {title}
                    </Typography.Text>
                  </Space>
                </Button>
              </List.Item>
            );
          }}
        />
      </div>

      <Divider style={{ margin: "12px 0" }} />
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        <Badge count={archivedCount} size="small" offset={[5, 0]}>
          <Button
            type={page === "trash" ? "default" : "text"}
            block
            icon={<DeleteOutlined />}
            onClick={() => onNavigate("trash")}
            aria-label={`回收站${archivedCount ? ` (${archivedCount})` : ""}`}
            style={{ textAlign: "left" }}
          >
            回收站
          </Button>
        </Badge>
        <Button
          type={page === "benchmark" ? "default" : "text"}
          block
          icon={<BarChartOutlined />}
          onClick={() => onNavigate("benchmark")}
          aria-label="Benchmark 成绩单"
          style={{ textAlign: "left" }}
        >
          Benchmark 成绩单
        </Button>
      </Space>

      <div style={{ marginTop: 16 }}>
        <Typography.Text type="secondary" style={{ display: "block", fontSize: 12 }}>
          Mini-Agent
        </Typography.Text>
        <Space style={{ width: "100%", justifyContent: "space-between", marginTop: 8 }}>
          <Typography.Text ellipsis title={user?.email} style={{ maxWidth: "calc(100% - 56px)" }}>
            {user?.email}
          </Typography.Text>
          <Button
            type="text"
            size="small"
            icon={<LogoutOutlined />}
            onClick={() => void onSignOut()}
            aria-label="退出"
          >
            退出
          </Button>
        </Space>
      </div>
    </div>
  );
}

export { confirmDelete };
