import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { compactTurn, listSkills } from "../../api";
import { HELP_TEXT, type CommandDefinition } from "../../commands";
import { completionText } from "../../commands/completion";
import type { Conversation, RuntimeStateNode } from "../../types";
import type { FileMentionEditorHandle } from "./FileMentionEditor";
import type { ChatMainView } from "./ChatToolbar";

interface UseChatCommandsOptions {
  activeCommandIndex: number;
  filteredCommands: CommandDefinition[];
  editorRef: MutableRefObject<FileMentionEditorHandle | null>;
  setActiveCommandIndex: Dispatch<SetStateAction<number>>;
  setCommandMenuDismissedFor: Dispatch<SetStateAction<string | null>>;
  setCompactionPending: Dispatch<SetStateAction<boolean>>;
  setMainView: Dispatch<SetStateAction<ChatMainView>>;
  compactionPending: boolean;
  isSubagent: boolean;
  conversation: Conversation | null;
  activeRuntimeNode?: RuntimeStateNode;
  clearComposer: () => void;
  onInsert: (content: string) => Promise<void>;
  onNew: (title?: string) => unknown | Promise<unknown>;
  onReload: (conversationId: string, preferredActiveTurnId?: string) => Promise<void>;
  onInfo: (content: string) => void;
}

export function useChatCommands({
  activeCommandIndex,
  filteredCommands,
  editorRef,
  setActiveCommandIndex,
  setCommandMenuDismissedFor,
  setCompactionPending,
  setMainView,
  compactionPending,
  isSubagent,
  conversation,
  activeRuntimeNode,
  clearComposer,
  onInsert,
  onNew,
  onReload,
  onInfo,
}: UseChatCommandsOptions) {
  function completeCommand(index = activeCommandIndex) {
    const command = filteredCommands[index];
    if (!command) return;
    const value = completionText(command);
    editorRef.current?.clear();
    editorRef.current?.insertText(value);
    setCommandMenuDismissedFor(value);
    setActiveCommandIndex(0);
  }

  async function executeCommand(name: string, argument: string) {
    if (compactionPending) return;
    clearComposer();
    setCommandMenuDismissedFor(null);
    setActiveCommandIndex(0);
    if (name === "/trace") {
      setMainView("trace");
      return;
    }
    if (name === "/help") {
      await onInsert(HELP_TEXT);
      return;
    }
    if (name === "/new") {
      await onNew(argument || undefined);
      return;
    }
    if (name === "/skills") {
      try {
        const skills = await listSkills();
        await onInsert(`# 已发现技能（${skills.length} 个）\n\n${skills.map((skill) => `- \`${skill.name}\` — ${skill.description}`).join("\n") || "（无）"}`);
      } catch (error) {
        await onInsert(`⚠️ 获取技能失败：${String((error as Error).message ?? error)}`);
      }
      return;
    }
    if (name !== "/compact") return;
    if (isSubagent) {
      onInfo("Subagent 仅使用自动上下文压缩。");
      return;
    }
    if (!conversation || !activeRuntimeNode) return;
    setCompactionPending(true);
    try {
      const compacted = await compactTurn(activeRuntimeNode.id);
      await onReload(conversation.id, compacted.id);
    } catch (error) {
      await onInsert(`⚠️ 压缩失败：${String((error as Error).message ?? error)}`);
    } finally {
      setCompactionPending(false);
    }
  }

  return { completeCommand, executeCommand };
}
