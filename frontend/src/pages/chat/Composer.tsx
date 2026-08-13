import { Button, Drawer, Input, Progress, Select, Space, Tooltip } from "antd";
import { ArrowUpOutlined, SettingOutlined, StopOutlined } from "@ant-design/icons";
import type { TextAreaRef } from "antd/es/input/TextArea";
import type { KeyboardEvent, RefObject } from "react";
import type { ChatMode, PermissionMode, ReasoningEffort } from "../../types";
import IconAction from "../../components/IconAction";

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
  providerName: string;
  currentModel: string;
  usagePercent?: number;
  usageTotalTokens?: number | null;
  usageContextLength?: number;
  openSettingsSelect: SettingsSelectKey | null;
  settingsOpen: boolean;
  taRef: RefObject<TextAreaRef>;
  onInputChange: (value: string) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onComplete: (index?: number) => void;
  onActiveCommandChange: (index: number) => void;
  onModeChange: (mode: ChatMode) => void;
  onPermissionChange: (mode: PermissionMode) => void;
  onReasoningChange: (effort: ReasoningEffort) => void;
  onProviderChange: (providerName: string) => void;
  onModelChange: (model: string) => void;
  onSettingsSelectChange: (key: SettingsSelectKey | null) => void;
  onOpenSettings: () => void;
  onCloseSettings: () => void;
  onStop: () => void;
  onSend: () => void;
  disabled?: boolean;
  disabledReason?: string;
}

export default function Composer(props: ComposerProps) {
  const settingsControls = (
    <Space className="composer-settings-controls" size={[6, 6]} wrap>
      <Select className="mode-picker" placement="topLeft" open={props.openSettingsSelect === "mode"} aria-label="运行模式" disabled={false} value={props.mode} options={[{ value: "agent", label: "⚙ Agent" }, { value: "plan", label: "📋 Plan" }]} onChange={props.onModeChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "mode" : null)} />
      <Select className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "permission"} aria-label="权限模式" disabled={false} value={props.permissionMode} options={[{ value: "approval_for_me", label: "逐次审批" }, { value: "full_access", label: "完全访问" }]} onChange={props.onPermissionChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "permission" : null)} />
      <Input size="small" aria-label="提供商" value={props.providerName} placeholder="Provider" onChange={(event) => props.onProviderChange(event.target.value)} />
      <Input size="small" aria-label="模型" value={props.currentModel} placeholder="Model" onChange={(event) => props.onModelChange(event.target.value)} />
      <Space size={4} align="center">
        <Tooltip title={props.usageTotalTokens == null ? "暂无 token usage" : `${props.usageTotalTokens.toLocaleString()} / ${(props.usageContextLength ?? 0).toLocaleString()} tokens`}>
          <Progress type="circle" size={32} percent={Math.max(0, Math.min(100, props.usagePercent ?? 0))} format={() => props.usageTotalTokens == null ? "–" : props.usageTotalTokens >= 1000 ? `${(props.usageTotalTokens / 1000).toFixed(1)}k` : String(props.usageTotalTokens)} />
        </Tooltip>
        <Select className="composer-picker" placement="topLeft" open={props.openSettingsSelect === "reasoning"} aria-label="思考等级" disabled={false} value={props.reasoningEffort} options={(Object.keys(REASONING_LABELS) as ReasoningEffort[]).map((level) => ({ value: level, label: `${level}` }))} onChange={props.onReasoningChange} onOpenChange={(open) => props.onSettingsSelectChange(open ? "reasoning" : null)} />
      </Space>
    </Space>
  );
  return (
    <div className="composer">
      {props.commandMenuVisible && <div className="command-menu">{props.filteredCommands.map((command, index) => <button key={command.name} className={`command-item${index === props.activeCommandIndex ? " selected" : ""}`} onMouseEnter={() => props.onActiveCommandChange(index)} onClick={() => props.onComplete(index)}><span className="command-name">{command.name}</span><span className="command-desc">{command.label} · {command.description}</span></button>)}</div>}
      <div className="composer-box">
        <Input.TextArea className="composer-input" ref={props.taRef} value={props.input} disabled={props.disabled} onChange={(event) => props.onInputChange(event.target.value)} onKeyDown={props.onKeyDown} placeholder={props.disabledReason || "输入任务，按 Enter 发送"} autoSize={{ minRows: 1, maxRows: 8 }} />
        <div className="composer-toolbar">{props.isMobile ? <IconAction className="run-settings-trigger" label="运行设置" icon={<SettingOutlined />} disabled={false} onClick={props.onOpenSettings} /> : settingsControls}</div>
        {props.busy ? <Tooltip title="停止"><Button className="send-btn stop" type="default" danger shape="circle" icon={<StopOutlined />} aria-label="停止" onClick={props.onStop} /> </Tooltip> : <Tooltip title={props.disabledReason || "发送"}><Button className="send-btn" type="primary" shape="circle" icon={<ArrowUpOutlined />} aria-label="发送" onClick={props.onSend} disabled={props.disabled || !props.input.trim()} /></Tooltip>}
      </div>
      <Drawer className="run-settings-drawer" title="运行设置" placement="bottom" open={props.settingsOpen} onClose={props.onCloseSettings}>{settingsControls}</Drawer>
    </div>
  );
}
