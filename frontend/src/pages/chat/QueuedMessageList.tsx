import { DeleteOutlined, EditOutlined, SendOutlined } from "@ant-design/icons";
import type { QueuedMessage } from "../../app/types";

interface Props {
  items: QueuedMessage[];
  onSend: (item: QueuedMessage) => void;
  onEdit: (item: QueuedMessage) => void;
  onDelete: (item: QueuedMessage) => void;
}

export default function QueuedMessageList({ items, onSend, onEdit, onDelete }: Props) {
  if (items.length === 0) return null;
  return (
    <section className="queued-message-list" aria-label="待发送消息">
      <div className="queued-message-header">待发送 {items.length} 条</div>
      <ol>
        {items.map((item, index) => (
          <li key={item.id} className="queued-message-item">
            <span className="queued-message-index" aria-hidden="true">{index + 1}</span>
            <span className="queued-message-content">
              {item.content || "（仅文件）"}
              {item.sendingSteeringId ? <span className="queued-message-sending"> · 发送中</span> : null}
            </span>
            <span className="queued-message-actions">
              <button type="button" className="queued-message-button" aria-label={`发送第 ${index + 1} 条待发送消息`} disabled={Boolean(item.sendingSteeringId)} onClick={() => onSend(item)}>
                <SendOutlined aria-hidden="true" />
              </button>
              <button type="button" className="queued-message-button" aria-label={`编辑第 ${index + 1} 条待发送消息`} disabled={Boolean(item.sendingSteeringId)} onClick={() => onEdit(item)}>
                <EditOutlined aria-hidden="true" />
              </button>
              <button type="button" className="queued-message-button danger" aria-label={`删除第 ${index + 1} 条待发送消息`} disabled={Boolean(item.sendingSteeringId)} onClick={() => onDelete(item)}>
                <DeleteOutlined aria-hidden="true" />
              </button>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
