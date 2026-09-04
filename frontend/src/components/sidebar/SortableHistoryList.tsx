import {
  DndContext,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { SortableContext, arrayMove, sortableKeyboardCoordinates, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { Empty } from "antd";
import type { Conversation } from "../../types";
import { HistoryRow } from "./ConversationHistory";
import type { HistoryMutationProps } from "./types";

interface SortableHistoryListProps extends HistoryMutationProps {
  conversations: Conversation[];
  currentId: string | null;
  disabled?: boolean;
  onSelect: (id: string) => void;
  onReorder: (orderedThreadIds: string[]) => Promise<void>;
  pageIsChat: boolean;
}

export function reorderHistoryIds(ids: string[], activeId: string, overId: string): string[] {
  const oldIndex = ids.indexOf(activeId);
  const newIndex = ids.indexOf(overId);
  return oldIndex < 0 || newIndex < 0 || oldIndex === newIndex ? ids : arrayMove(ids, oldIndex, newIndex);
}

export function SortableHistoryList({
  conversations,
  currentId,
  disabled = false,
  onSelect,
  onRename,
  onArchive,
  onDelete,
  onReorder,
  pageIsChat,
}: SortableHistoryListProps) {
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 180, tolerance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const ids = conversations.map((conversation) => conversation.id);

  function finishDrag(event: DragEndEvent) {
    const activeId = String(event.active.id);
    const overId = event.over ? String(event.over.id) : null;
    if (!overId || activeId === overId || disabled) return;
    const next = reorderHistoryIds(ids, activeId, overId);
    if (next === ids) return;
    void onReorder(next);
  }

  return (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={finishDrag}>
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {conversations.length > 0 ? (
          <ul className="history-list" aria-label="对话列表">
            {conversations.map((conversation) => (
              <HistoryRow
                key={conversation.id}
                conversation={conversation}
                selected={conversation.id === currentId && pageIsChat}
                dragDisabled={disabled || conversations.length < 2}
                onSelect={onSelect}
                onRename={onRename}
                onArchive={onArchive}
                onDelete={onDelete}
              />
            ))}
          </ul>
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对话" />
        )}
      </SortableContext>
    </DndContext>
  );
}
