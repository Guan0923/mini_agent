import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  onRestore: (id: string) => void;
  onDelete: (id: string) => void;
}

export default function TrashPage({ conversations, onRestore, onDelete }: Props) {
  return (
    <section className="trash-page">
      <div className="page-header">
        <h1>回收站</h1>
        <p>归档的对话会保留在这里；删除后仅从界面隐藏，后端仍保留审计记录。</p>
      </div>
      {conversations.length === 0 ? (
        <div className="empty-state">回收站是空的</div>
      ) : (
        <div className="trash-list">
          {conversations.map((conversation) => (
            <article className="trash-card" key={conversation.id}>
              <div>
                <h2>{conversation.title || "新对话"}</h2>
                <p>
                  {conversation.archivedAt
                    ? `归档于 ${new Date(conversation.archivedAt).toLocaleString()}`
                    : "已归档"}
                </p>
              </div>
              <div className="trash-actions">
                <button type="button" onClick={() => onRestore(conversation.id)}>
                  恢复
                </button>
                <button type="button" className="danger-text" onClick={() => onDelete(conversation.id)}>
                  删除
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
