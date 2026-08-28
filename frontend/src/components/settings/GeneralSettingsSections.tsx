import { Alert, Form, Input, InputNumber, Select, Switch, Typography } from "antd";
import type { UserSettingsState } from "./useUserSettingsState";

type SectionProps = { state: UserSettingsState };

export function ProfileSettingsSection({ state }: SectionProps) {
  const settings = state.settings!;
  return (
    <Form layout="vertical">
      <Typography.Title level={4}>个人简介</Typography.Title>
      <Form.Item label="用户名">
        <Input
          aria-label="用户名"
          maxLength={80}
          value={settings.profile.display_name}
          onChange={(event) => state.updateSettings({
            profile: { ...settings.profile, display_name: event.target.value },
          })}
        />
      </Form.Item>
      <Form.Item label="兼容偏好文本">
        <Input.TextArea
          aria-label="兼容偏好文本"
          maxLength={4000}
          autoSize={{ minRows: 3, maxRows: 8 }}
          value={settings.profile.agent_preferences}
          onChange={(event) => state.updateSettings({
            profile: { ...settings.profile, agent_preferences: event.target.value },
          })}
        />
      </Form.Item>
    </Form>
  );
}

export function AgentSettingsSection({ state }: SectionProps) {
  const settings = state.settings!;
  return (
    <Form layout="vertical">
      <Typography.Title level={4}>Agent 配置</Typography.Title>
      <Form.Item label="语气">
        <Select
          value={settings.agent_config.tone}
          options={[
            { value: "balanced", label: "平衡" },
            { value: "formal", label: "正式" },
            { value: "friendly", label: "友好" },
            { value: "direct", label: "直接" },
          ]}
          onChange={(tone) => state.updateSettings({ agent_config: { ...settings.agent_config, tone } })}
        />
      </Form.Item>
      <Form.Item label="回答风格">
        <Select
          value={settings.agent_config.verbosity}
          options={[
            { value: "balanced", label: "平衡" },
            { value: "concise", label: "简洁" },
            { value: "detailed", label: "详细" },
          ]}
          onChange={(verbosity) => state.updateSettings({ agent_config: { ...settings.agent_config, verbosity } })}
        />
      </Form.Item>
      <Form.Item label="运行信息详略">
        <Select
          aria-label="运行信息详略"
          value={!import.meta.env.DEV && settings.agent_config.display_mode === "developer" ? "verbose" : settings.agent_config.display_mode}
          options={[
            { value: "minimal", label: "简洁" },
            { value: "medium", label: "标准" },
            { value: "verbose", label: "详细" },
            ...(import.meta.env.DEV ? [{ value: "developer", label: "开发者" }] : []),
          ]}
          onChange={(display_mode) => state.updateSettings({ agent_config: { ...settings.agent_config, display_mode } })}
        />
      </Form.Item>
      <Form.Item label="时区">
        <Select
          aria-label="Agent 默认时区"
          showSearch={{ optionFilterProp: "label" }}
          value={settings.agent_config.timezone}
          options={(settings.timezone_options ?? []).map((option) => ({
            value: option.identifier,
            label: `${option.label} (${option.identifier})`,
          }))}
          onChange={(timezone) => {
            state.setLocationError("");
            state.updateSettings({ agent_config: { ...settings.agent_config, timezone, location_enabled: false } });
          }}
          notFoundContent="暂无可用时区"
        />
      </Form.Item>
      <Form.Item label="允许获取定位以自动设置时区">
        <Switch checked={settings.agent_config.location_enabled} onChange={(checked) => void state.toggleLocation(checked)} />
      </Form.Item>
      {state.locationError ? <Typography.Text type="danger">{state.locationError}</Typography.Text> : null}
      <Form.Item label="主动性">
        <Select
          value={settings.agent_config.initiative}
          options={[
            { value: "balanced", label: "平衡" },
            { value: "reserved", label: "克制" },
            { value: "proactive", label: "主动" },
          ]}
          onChange={(initiative) => state.updateSettings({ agent_config: { ...settings.agent_config, initiative } })}
        />
      </Form.Item>
      <Form.Item label="自由文本偏好">
        <Input.TextArea
          aria-label="自由文本偏好"
          maxLength={4000}
          autoSize={{ minRows: 7, maxRows: 14 }}
          value={settings.agent_config.custom_instructions}
          onChange={(event) => state.updateSettings({
            agent_config: { ...settings.agent_config, custom_instructions: event.target.value },
          })}
        />
      </Form.Item>
    </Form>
  );
}

export function RuntimeSettingsSection({ state }: SectionProps) {
  const settings = state.settings!;
  return (
    <Form layout="vertical">
      <Typography.Title level={4}>运行配置</Typography.Title>
      {settings.terminal_notice ? (
        <Alert type="warning" showIcon title="终端状态提示" description={settings.terminal_notice} style={{ marginBottom: 16 }} />
      ) : null}
      <Form.Item label="启动终端">
        <Select
          aria-label="启动终端"
          value={settings.runtime_config.terminal_type}
          options={settings.terminal_options}
          disabled={settings.terminal_options.length === 0}
          onChange={(terminal_type) => state.updateSettings({
            runtime_config: { ...settings.runtime_config, terminal_type },
          })}
          notFoundContent="暂无可用终端"
        />
        <Typography.Paragraph type="secondary">
          仅显示当前系统已安装且可调用的终端；保存后仅影响新建或恢复的运行。
        </Typography.Paragraph>
      </Form.Item>
      <Form.Item label="工具调用上限">
        <InputNumber
          aria-label="工具调用上限"
          min={1}
          max={1000}
          step={1}
          precision={0}
          value={settings.runtime_config.max_tool_calls}
          onChange={(max_tool_calls) => {
            if (typeof max_tool_calls === "number" && Number.isInteger(max_tool_calls)) {
              state.updateSettings({ runtime_config: { ...settings.runtime_config, max_tool_calls } });
            }
          }}
        />
        <Typography.Paragraph type="secondary">
          默认值为 32。成功、失败和重复的工具调用都会计入整个 Agent 工作流的上限；保存后仅影响新建或恢复的运行。
        </Typography.Paragraph>
      </Form.Item>
    </Form>
  );
}
