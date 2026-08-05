import { Alert, App as AntApp, Button, Empty, List, Modal, Space, Typography } from "antd";
import { DeleteOutlined, InfoCircleOutlined, UndoOutlined } from "@ant-design/icons";
import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  onRestore: (id: string) => void | Promise<void>;
  onDelete: (id: string) => void | Promise<void>;
}

export default function TrashPage({ conversations, onRestore, onDelete }: Props) {
  const { modal } = AntApp.useApp();
  const confirmModal = modal && typeof modal.confirm === "function" ? modal.confirm.bind(modal) : Modal.confirm;

  function confirmDelete(conversation: Conversation) {
    const title = conversation.title || "新对话";
    confirmModal({
      title: `删除“${title}”？`,
      content: "删除后将从界面隐藏，但后台仍保留审计数据。确定继续吗？",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => onDelete(conversation.id),
    });
  }

  return (
    <section className="trash-page" style={{ maxWidth: 960 }}>
      <Typography.Title level={1} style={{ marginTop: 0 }}>
        回收站
      </Typography.Title>
      <Alert
        type="info"
        showIcon
        icon={<InfoCircleOutlined />}
        message="归档的对话会保留在这里；删除后仅从界面隐藏，后端仍保留审计记录。"
        style={{ marginBottom: 20 }}
      />
      {conversations.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="回收站是空的" />
      ) : (
        <List
          className="trash-list"
          bordered
          dataSource={conversations}
          renderItem={(conversation) => (
            <List.Item
              className="trash-card"
              key={conversation.id}
              actions={[
                <Space key={`${conversation.id}-actions`}>
                  <Button
                    type="default"
                    icon={<UndoOutlined />}
                    onClick={() => void onRestore(conversation.id)}
                    aria-label="恢复"
                  >
                    恢复
                  </Button>
                  <Button
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => confirmDelete(conversation)}
                    aria-label="删除"
                  >
                    删除
                  </Button>
                </Space>,
              ]}
            >
              <List.Item.Meta
                title={<Typography.Title level={4} style={{ margin: 0 }}>{conversation.title || "新对话"}</Typography.Title>}
                description={
                  conversation.archivedAt
                    ? `归档于 ${new Date(conversation.archivedAt).toLocaleString()}`
                    : "已归档"
                }
              />
            </List.Item>
          )}
        />
      )}
    </section>
  );
}
