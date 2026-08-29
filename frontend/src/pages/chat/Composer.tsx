import { Button, Dropdown, Progress, Select, Space, Tooltip } from "antd";
import {
  ArrowUpOutlined,
  BulbOutlined,
  PaperClipOutlined,
  PauseCircleTwoTone,
  PlayCircleTwoTone,
  RobotOutlined,
  SafetyOutlined,
} from "@ant-design/icons";
import { useRef, useState, type KeyboardEvent, type RefObject } from "react";
import type { ChatMode, PermissionMode, ReasoningEffort, TodoItem } from "../../types";
import IconAction from "../../components/IconAction";
import type { FileCandidate } from "../../commands/fileCompletion";
import { sessionFileContentUrl } from "../../api/projects/files";
import { SessionTodoPanel } from "./todoPanel";
import FileMentionEditor, { type FileMentionChange, type FileMentionEditorHandle } from "./FileMentionEditor";
import QueuedMessageList from "./QueuedMessageList";
import type { QueuedMessage } from "../../app/types";

const REASONING_LABELS: Record<ReasoningEffort, string> = { low: "低", medium: "中", high: "高", xhigh: "超高", max: "最大" };
const MODE_LABELS: Record<ChatMode, string> = { agent: "Agent", plan: "Plan" };
const PERMISSION_LABELS: Record<PermissionMode, string> = {
  read_only: "只读",
  workspace_write: "工作区读写",
  full_access: "完全访问",
};
export type SettingsSelectKey = "mode" | "permission" | "reasoning";
export type ComposerActionMode = "send" | "pause" | "resume";

export interface ComposerProps {
  input: string;
  busy: boolean;
  actionMode?: ComposerActionMode;
  submitDisabled?: boolean;
  startMode?: boolean;
  compact: boolean;
  filteredCommands: Array<{ name: string; label: string; description: string }>;
  commandMenuVisible: boolean;
  activeCommandIndex: number;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  modePending?: boolean;
  permissionPending?: boolean;
  reasoningPending?: boolean;
  todos: TodoItem[] | null;
  todoClosable?: boolean;
  onTodoClose?: () => void;
  usagePercent?: number;
  usageTotalTokens?: number | null;
  usageContextLength?: number;
  openSettingsSelect: SettingsSelectKey | null;
  editorRef: RefObject<FileMentionEditorHandle>;
  sessionId?: string;
  onEditorChange: (change: FileMentionChange) => void;
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  onComplete: (index?: number) => void;
  onActiveCommandChange: (index: number) => void;
  onModeChange: (mode: ChatMode) => void;
  onPermissionChange: (mode: PermissionMode) => void;
  onReasoningChange: (effort: ReasoningEffort) => void;
  onSettingsSelectChange: (key: SettingsSelectKey | null) => void;
  onStop: () => void;
  onSend: () => void;
  disabled?: boolean;
  disabledReason?: string;
  // File references: completion menu + inline editor nodes.
  fileCandidates: FileCandidate[];
  fileMenuVisible: boolean;
  activeFileIndex: number;
  fileMenuQuery: string;
  onFileComplete: (index?: number) => void;
  onActiveFileChange: (index: number) => void;
  onPickFiles: (files: FileList | File[]) => void;
  uploadsDisabled?: boolean;
  pendingUploads: Array<{
    uid: string;
    name: string;
    isImage: boolean;
    status: "uploading" | "done" | "error";
    percent: number;
    path?: string;
    error?: string;
  }>;
  onRemoveUpload: (index: number) => void;
  onRetryUpload: (index: number) => void;
  onUploadPreview: (index: number) => void;
  uploadsUploading?: boolean;
  queuedMessages?: QueuedMessage[];
  onQueueSend?: (item: QueuedMessage) => void;
  onQueueEdit?: (item: QueuedMessage) => void;
  onQueueDelete?: (item: QueuedMessage) => void;
}

export default function Composer(props: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const actionMode = props.actionMode ?? (props.busy ? "pause" : "send");

  function openFilePicker() {
    if (props.disabled || props.uploadsDisabled) return;
    fileInputRef.current?.click();
  }

  function handleFilesChange(event: React.ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (files && files.length > 0) props.onPickFiles(files);
    event.target.value = "";
  }

  function handlePaste(event: globalThis.ClipboardEvent) {
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length > 0 && !props.disabled && !props.uploadsDisabled) {
      event.preventDefault();
      props.onPickFiles(files);
    }
  }

  function handleDrop(event: React.DragEvent<HTMLDivElement>) {
    setDragOver(false);
    const files = event.dataTransfer?.files;
    if (files && files.length > 0 && !props.disabled && !props.uploadsDisabled) {
      event.preventDefault();
      props.onPickFiles(files);
    }
  }

  const usageIndicator = (
    <Tooltip title={props.usageTotalTokens == null ? "暂无 token usage" : `${props.usageTotalTokens.toLocaleString()} / ${(props.usageContextLength ?? 0).toLocaleString()} tokens`}>
      <Progress className={props.compact ? "composer-compact-usage" : undefined} type="circle" size={props.compact ? 28 : 32} percent={Math.max(0, Math.min(100, props.usagePercent ?? 0))} format={() => props.usageTotalTokens == null ? "–" : props.usageTotalTokens >= 1000 ? `${(props.usageTotalTokens / 1000).toFixed(1)}k` : String(props.usageTotalTokens)} />
    </Tooltip>
  );
  const settingsControls = (
    <Space className="composer-settings-controls" size={[6, 6]} wrap>
      <Select virtual={false} className="mode-picker" placement="topLeft" open={props.openSettingsSelect === "mode"} aria-label="运行模式" loading={props.modePending} disabled={props.disabled || props.modePending} value={props.mode} options={[{ value: "agent", label: "⚙ Agent" }, { value: "plan", label: "📋 Plan" }]} onChange={props.onModeChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "mode" : null)} />
      <Select virtual={false} className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "permission"} aria-label="权限模式" loading={props.permissionPending} disabled={props.disabled || props.permissionPending} value={props.permissionMode} options={[{ value: "read_only", label: "只读" }, { value: "workspace_write", label: "工作区读写" }, { value: "full_access", label: "完全访问" }]} onChange={props.onPermissionChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "permission" : null)} />
      <Space size={4} align="center">
        {usageIndicator}
        <Select virtual={false} className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "reasoning"} aria-label="思考等级" loading={props.reasoningPending} disabled={props.disabled || props.reasoningPending} value={props.reasoningEffort} options={(Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((level) => ({ value: level, label: `${level}` }))} onChange={props.onReasoningChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "reasoning" : null)} />
      </Space>
    </Space>
  );
  const compactSettingsControls = (
    <Space className="composer-compact-settings" size={2}>
      <Tooltip title={`运行模式：${MODE_LABELS[props.mode]}`}>
        <Dropdown
          trigger={["click"]}
          placement="topLeft"
          open={props.openSettingsSelect === "mode"}
          onOpenChange={(open) => props.onSettingsSelectChange(open ? "mode" : null)}
          menu={{
            selectable: true,
            selectedKeys: [props.mode],
            items: (Object.keys(MODE_LABELS) as ChatMode[]).map((value) => ({
              key: value,
              label: MODE_LABELS[value],
              onClick: () => props.onModeChange(value),
            })),
          }}
        >
          <Button type="text" size="small" className="composer-compact-setting" icon={<RobotOutlined />} aria-label={`运行模式：${MODE_LABELS[props.mode]}`} loading={props.modePending} disabled={props.disabled || props.modePending} />
        </Dropdown>
      </Tooltip>
      <Tooltip title={`权限模式：${PERMISSION_LABELS[props.permissionMode]}`}>
        <Dropdown
          trigger={["click"]}
          placement="topLeft"
          open={props.openSettingsSelect === "permission"}
          onOpenChange={(open) => props.onSettingsSelectChange(open ? "permission" : null)}
          menu={{
            selectable: true,
            selectedKeys: [props.permissionMode],
            items: (Object.keys(PERMISSION_LABELS) as PermissionMode[]).map((value) => ({
              key: value,
              label: PERMISSION_LABELS[value],
              onClick: () => props.onPermissionChange(value),
            })),
          }}
        >
          <Button type="text" size="small" className="composer-compact-setting" icon={<SafetyOutlined />} aria-label={`权限模式：${PERMISSION_LABELS[props.permissionMode]}`} loading={props.permissionPending} disabled={props.disabled || props.permissionPending} />
        </Dropdown>
      </Tooltip>
      {usageIndicator}
      <Tooltip title={`思考等级：${REASONING_LABELS[props.reasoningEffort]}`}>
        <Dropdown
          trigger={["click"]}
          placement="topLeft"
          open={props.openSettingsSelect === "reasoning"}
          onOpenChange={(open) => props.onSettingsSelectChange(open ? "reasoning" : null)}
          menu={{
            selectable: true,
            selectedKeys: [props.reasoningEffort],
            items: (Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((value) => ({
              key: value,
              label: `${REASONING_LABELS[value]}（${value}）`,
              onClick: () => props.onReasoningChange(value),
            })),
          }}
        >
          <Button type="text" size="small" className="composer-compact-setting" icon={<BulbOutlined />} aria-label={`思考等级：${REASONING_LABELS[props.reasoningEffort]}`} loading={props.reasoningPending} disabled={props.disabled || props.reasoningPending} />
        </Dropdown>
      </Tooltip>
    </Space>
  );
  return (
    <div
      className={`composer composer-reveal-shell${props.todos && props.todos.length > 0 ? " has-todo" : ""}`}
      data-composer-seat
    >
      {props.fileMenuVisible && (
        <div className="command-menu file-menu" role="listbox" aria-label="文件补全">
          {props.fileCandidates.length === 0 ? (
            <div className="command-menu-empty">没有匹配的文件（“{props.fileMenuQuery}”）</div>
          ) : (
            props.fileCandidates.map((candidate, index) => (
              <button
                key={`${candidate.reference.source}:${candidate.reference.path}`}
                className={`command-item file-item${index === props.activeFileIndex ? " selected" : ""}`}
                onMouseEnter={() => props.onActiveFileChange(index)}
                onClick={() => props.onFileComplete(index)}
              >
                <span className="file-item-name">{candidate.label}</span>
                <span className="file-item-path">{candidate.reference.path}</span>
                <span className={`file-source-badge ${candidate.reference.source}`}>{candidate.sourceLabel}</span>
              </button>
            ))
          )}
        </div>
      )}
      {props.commandMenuVisible && <div className="command-menu">{props.filteredCommands.map((command, index) => <button key={command.name} className={`command-item${index === props.activeCommandIndex ? " selected" : ""}`} onMouseEnter={() => props.onActiveCommandChange(index)} onClick={() => props.onComplete(index)}><span className="command-name">{command.name}</span><span className="command-desc">{command.label} · {command.description}</span></button>)}</div>}
      <div className="composer-box-anchor">
        <QueuedMessageList
          items={props.queuedMessages ?? []}
          disabled={props.disabled}
          onSend={(item) => props.onQueueSend?.(item)}
          onEdit={(item) => props.onQueueEdit?.(item)}
          onDelete={(item) => props.onQueueDelete?.(item)}
        />
        {props.pendingUploads.length > 0 ? (
          <div className="composer-uploads composer-reveal-item" data-reveal-index="1" aria-label="上传进度">
            {props.pendingUploads.map((upload, index) => (
              <span key={upload.uid} className={`composer-upload status-${upload.status}`}>
                {upload.isImage && upload.status === "done" && upload.path && props.sessionId ? (
                  <img className="composer-upload-thumb" src={sessionFileContentUrl(props.sessionId, "upload", upload.path)} alt={upload.name} onClick={() => props.onUploadPreview(index)} />
                ) : <span className="composer-upload-icon">📄</span>}
                <span className="composer-upload-name" title={upload.name}>{upload.name}</span>
                {upload.status === "uploading" ? <Progress size="small" percent={upload.percent} showInfo={false} className="composer-upload-progress" /> : null}
                {upload.status === "error" ? <span className="composer-upload-error" title={upload.error}>上传失败</span> : null}
                {upload.status === "error" ? <Button type="text" size="small" onClick={() => props.onRetryUpload(index)}>重试</Button> : null}
                <Button type="text" size="small" className="composer-upload-remove" aria-label={`移除 ${upload.name}`} onClick={() => props.onRemoveUpload(index)}>×</Button>
              </span>
            ))}
          </div>
        ) : null}
        {props.todos && props.todos.length > 0 ? (
          <div className="composer-todo-anchor composer-reveal-item" data-reveal-index="1">
            <SessionTodoPanel
              key={props.todoClosable ? "closable" : "active"}
              todos={props.todos}
              busy={props.busy && !props.todoClosable}
              closable={props.todoClosable}
              onClose={props.onTodoClose}
            />
          </div>
        ) : null}
        <div className={`composer-box composer-reveal-item${dragOver ? " is-dragging" : ""}`} data-reveal-index="2" onDragOver={(event) => { if (!props.disabled) { event.preventDefault(); setDragOver(true); } }} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}>
          <input ref={fileInputRef} type="file" multiple hidden aria-hidden="true" onChange={handleFilesChange} />
          <FileMentionEditor
            ref={props.editorRef}
            disabled={props.disabled}
            placeholder={props.disabledReason || "输入任务，按 Enter 发送"}
            onChange={props.onEditorChange}
            onKeyDown={props.onKeyDown}
            onPasteFiles={handlePaste}
          />
          <div className="composer-toolbar composer-reveal-item" data-reveal-index="4">
            <IconAction className="file-upload-trigger" label="上传文件" icon={<PaperClipOutlined />} disabled={props.disabled || props.uploadsDisabled} onClick={openFilePicker} />
            {props.compact ? compactSettingsControls : settingsControls}
          </div>
          {actionMode === "pause" ? (
            <button
              className="send-btn stop composer-reveal-item"
              data-reveal-index="5"
              type="button"
              aria-label="暂停"
              onClick={props.onStop}
              disabled={props.submitDisabled}
            >
              <PauseCircleTwoTone aria-hidden="true" />
            </button>
          ) : actionMode === "resume" ? (
            <button
              className="send-btn composer-reveal-item"
              data-reveal-index="5"
              type="button"
              aria-label="继续"
              onClick={props.onSend}
              disabled={props.submitDisabled}
            >
              <PlayCircleTwoTone aria-hidden="true" />
            </button>
          ) : (
            <button
              className="send-btn composer-reveal-item"
              data-reveal-index="5"
              type="button"
              aria-label={props.startMode ? "开始" : "发送"}
              onClick={props.onSend}
              disabled={props.submitDisabled ?? (props.disabled || props.uploadsUploading || (!props.startMode && !props.input.trim() && props.pendingUploads.every((upload) => upload.status !== "done")))}
            >
              <ArrowUpOutlined aria-hidden="true" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
