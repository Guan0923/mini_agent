import { Button, Drawer, Input, Progress, Select, Space, Tooltip } from "antd";
import { ArrowUpOutlined, PaperClipOutlined, SettingOutlined, StopOutlined } from "@ant-design/icons";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { useRef, useState, type ChangeEvent, type ClipboardEvent, type DragEvent, type KeyboardEvent, type RefObject } from "react";
import type { ChatMode, FileReference, PermissionMode, ReasoningEffort, TodoItem } from "../../types";
import IconAction from "../../components/IconAction";
import type { FileCandidate } from "../../commands/fileCompletion";
import { sessionFileContentUrl } from "../../api/files";
import { SessionTodoPanel } from "./todoPanel";

const REASONING_LABELS: Record<ReasoningEffort, string> = { low: "低", medium: "中", high: "高", xhigh: "超高", max: "最大" };
export type SettingsSelectKey = "mode" | "permission" | "reasoning";

export interface ComposerProps {
  input: string;
  busy: boolean;
  isMobile: boolean;
  filteredCommands: Array<{ name: string; label: string; description: string }>;
  commandMenuVisible: boolean;
  activeCommandIndex: number;
  mode: ChatMode;
  permissionMode: PermissionMode;
  reasoningEffort: ReasoningEffort;
  todos: TodoItem[] | null;
  usagePercent?: number;
  usageTotalTokens?: number | null;
  usageContextLength?: number;
  openSettingsSelect: SettingsSelectKey | null;
  settingsOpen: boolean;
  taRef: RefObject<TextAreaRef>;
  sessionId?: string;
  onInputChange: (value: string) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onComplete: (index?: number) => void;
  onActiveCommandChange: (index: number) => void;
  onModeChange: (mode: ChatMode) => void;
  onPermissionChange: (mode: PermissionMode) => void;
  onReasoningChange: (effort: ReasoningEffort) => void;
  onSettingsSelectChange: (key: SettingsSelectKey | null) => void;
  onOpenSettings: () => void;
  onCloseSettings: () => void;
  onStop: () => void;
  onSend: () => void;
  disabled?: boolean;
  disabledReason?: string;
  // File references: completion menu + pending reference strip.
  fileCandidates: FileCandidate[];
  fileMenuVisible: boolean;
  activeFileIndex: number;
  fileMenuQuery: string;
  references: FileReference[];
  onFileComplete: (index?: number) => void;
  onActiveFileChange: (index: number) => void;
  onRemoveReference: (index: number) => void;
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
}

export default function Composer(props: ComposerProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [dragOver, setDragOver] = useState(false);

  function openFilePicker() {
    if (props.disabled || props.uploadsDisabled) return;
    fileInputRef.current?.click();
  }

  function handleFilesChange(event: React.ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (files && files.length > 0) props.onPickFiles(files);
    event.target.value = "";
  }

  function handlePaste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
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

  const settingsControls = (
    <Space className="composer-settings-controls" size={[6, 6]} wrap>
      <Select className="mode-picker" placement="topLeft" open={props.openSettingsSelect === "mode"} aria-label="运行模式" disabled={false} value={props.mode} options={[{ value: "agent", label: "⚙ Agent" }, { value: "plan", label: "📋 Plan" }]} onChange={props.onModeChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "mode" : null)} />
      <Select className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "permission"} aria-label="权限模式" disabled={false} value={props.permissionMode} options={[{ value: "approval_for_me", label: "逐次审批" }, { value: "full_access", label: "完全访问" }]} onChange={props.onPermissionChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "permission" : null)} />
      <Space size={4} align="center">
        <Tooltip title={props.usageTotalTokens == null ? "暂无 token usage" : `${props.usageTotalTokens.toLocaleString()} / ${(props.usageContextLength ?? 0).toLocaleString()} tokens`}>
          <Progress type="circle" size={32} percent={Math.max(0, Math.min(100, props.usagePercent ?? 0))} format={() => props.usageTotalTokens == null ? "–" : props.usageTotalTokens >= 1000 ? `${(props.usageTotalTokens / 1000).toFixed(1)}k` : String(props.usageTotalTokens)} />
        </Tooltip>
        <Select className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "reasoning"} aria-label="思考等级" disabled={false} value={props.reasoningEffort} options={(Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((level) => ({ value: level, label: `${level}` }))} onChange={props.onReasoningChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "reasoning" : null)} />
      </Space>
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
        {props.references.length > 0 ? (
          <div className="composer-references composer-reveal-item" data-reveal-index="1" aria-label="待发送引用">
            {props.references.map((reference, index) => (
              <span key={`${reference.source}:${reference.path}`} className="composer-reference">
                <span className={`file-source-badge ${reference.source}`}>{reference.source === "upload" ? "会话上传" : "项目文件"}</span>
                <span className="composer-reference-path">{reference.path}</span>
                <Button type="text" size="small" className="composer-reference-remove" aria-label={`移除引用 ${reference.path}`} onClick={() => props.onRemoveReference(index)}>×</Button>
              </span>
            ))}
          </div>
        ) : null}
        {props.todos && props.todos.length > 0 ? (
          <div className="composer-todo-anchor composer-reveal-item" data-reveal-index="1">
            <SessionTodoPanel todos={props.todos} busy={props.busy} />
          </div>
        ) : null}
        <div className={`composer-box composer-reveal-item${dragOver ? " is-dragging" : ""}`} data-reveal-index="2" onDragOver={(event) => { if (!props.disabled) { event.preventDefault(); setDragOver(true); } }} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}>
          <input ref={fileInputRef} type="file" multiple hidden aria-hidden="true" onChange={handleFilesChange} />
          <Input.TextArea className="composer-input composer-reveal-item" data-reveal-index="3" ref={props.taRef} value={props.input} disabled={props.disabled} onChange={(event) => props.onInputChange(event.target.value)} onKeyDown={props.onKeyDown} onPaste={handlePaste} placeholder={props.disabledReason || "输入任务，按 Enter 发送"} autoSize={{ minRows: 1, maxRows: 8 }} />
          <div className="composer-toolbar composer-reveal-item" data-reveal-index="4">
            <IconAction className="file-upload-trigger" label="上传文件" icon={<PaperClipOutlined />} disabled={props.disabled || props.uploadsDisabled} onClick={openFilePicker} />
            {props.isMobile ? <IconAction className="run-settings-trigger" label="运行设置" icon={<SettingOutlined />} disabled={false} onClick={props.onOpenSettings} /> : settingsControls}
          </div>
          {props.busy ? <Tooltip title="停止"><Button className="send-btn stop composer-reveal-item" data-reveal-index="5" type="default" danger shape="circle" icon={<StopOutlined />} aria-label="停止" onClick={props.onStop} /> </Tooltip> : <Tooltip title={props.disabledReason || "发送"}><Button className="send-btn composer-reveal-item" data-reveal-index="5" type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="发送" onClick={props.onSend} disabled={props.disabled || (!props.input.trim() && props.references.length === 0)} /></Tooltip>}
        </div>
      </div>
      <Drawer className="run-settings-drawer" title="运行设置" placement="bottom" open={props.settingsOpen} onClose={props.onCloseSettings}>{settingsControls}</Drawer>
    </div>
  );
}
