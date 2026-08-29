import { Alert, Button, Form, Input, InputNumber, Select, Space, Typography } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import type { SandboxLimits, SandboxNetworkRule } from "../../api";
import type { UserSettingsState } from "./useUserSettingsState";

type SectionProps = { state: UserSettingsState };

const brokerErrorTitles: Record<string, string> = {
  broker_unavailable: "沙箱 Broker 不可用",
  broker_not_installed: "沙箱 Broker 未安装",
  broker_service_configuration_invalid: "Broker 服务配置异常",
  broker_ready_marker_unavailable: "Broker 就绪信息缺失",
  broker_ready_marker_invalid: "Broker 就绪信息异常",
  broker_proxy_configuration_invalid: "Broker 代理配置异常",
  broker_installation_key_missing: "Broker 安装密钥缺失",
  broker_pipe_unavailable: "Broker 通信失败",
  broker_protocol_incompatible: "Broker 协议版本不兼容",
  broker_token_model_incompatible: "Broker 令牌模型不兼容",
  broker_generation_mismatch: "Broker 配置代际不一致",
  broker_response_invalid: "Broker 响应异常",
  broker_response_authentication_failed: "Broker 响应身份验证失败",
  broker_unhealthy: "Broker 健康检查未通过",
  broker_status_failed: "Broker 状态检查失败",
  broker_uac_cancelled: "Broker 修复授权已取消",
  broker_admin_required: "Broker 修复需要管理员权限",
  broker_dependency_missing: "Broker 修复依赖缺失",
  broker_account_failed: "Broker 沙箱账户配置失败",
  broker_credential_failed: "Broker 沙箱账户凭据失败",
  broker_privilege_failed: "Broker 权限配置失败",
  broker_network_failed: "Broker 网络隔离配置失败",
  broker_acl_failed: "Broker 文件权限配置失败",
  broker_service_failed: "Broker Windows 服务配置失败",
  broker_service_stop_failed: "Broker Windows 服务停止失败",
  broker_service_start_failed: "Broker Windows 服务启动失败",
  broker_not_ready: "Broker 修复后未就绪",
  broker_install_failed: "Broker 修复失败",
};

export function brokerErrorTitle(code: string | null, installed: boolean): string {
  return (code && brokerErrorTitles[code]) || (installed ? "沙箱 Broker 异常" : "沙箱 Broker 未安装");
}

const limitFields: Array<{
  key: keyof SandboxLimits;
  label: string;
  min: number;
  max: number;
  hint: string;
}> = [
  { key: "wall_seconds", label: "最长运行时间（秒）", min: 1, max: 300, hint: "1–300" },
  { key: "cpu_seconds", label: "CPU 时间（秒）", min: 1, max: 300, hint: "1–300" },
  { key: "memory_mib", label: "内存（MiB）", min: 128, max: 4096, hint: "128–4096" },
  { key: "processes", label: "进程数", min: 1, max: 256, hint: "1–256" },
  { key: "handles", label: "句柄数", min: 64, max: 16384, hint: "64–16384" },
  { key: "output_chars", label: "输出字符数", min: 1000, max: 20000, hint: "1000–20000" },
  { key: "disk_mib", label: "磁盘写入（MiB）", min: 0, max: 20480, hint: "0 表示不额外限制，最大 20480" },
];

export function SandboxSettingsSection({ state }: SectionProps) {
  const settings = state.settings!;
  const config = settings.sandbox_config;

  function updateAllowlist(index: number, patch: Partial<SandboxNetworkRule>) {
    state.updateSettings({
      sandbox_config: {
        ...config,
        network_allowlist: config.network_allowlist.map((rule, ruleIndex) => (
          ruleIndex === index ? { ...rule, ...patch } : rule
        )),
      },
    });
  }

  return (
    <Form layout="vertical">
      <Typography.Title level={4}>沙箱</Typography.Title>
      <Space style={{ marginBottom: 16 }} wrap>
        <Button loading={state.sandboxHealth.checking} onClick={() => void state.sandboxHealth.check()}>
          检查
        </Button>
        {state.sandboxHealth.phase === "unhealthy" ? (
          <Button
            type="primary"
            danger
            loading={state.sandboxHealth.repairing}
            disabled={state.sandboxHealth.checking}
            onClick={() => void state.sandboxHealth.repair()}
          >
            修复
          </Button>
        ) : null}
      </Space>
      {state.sandboxHealth.phase === "unhealthy" ? (
        <Alert
          type="error"
          showIcon
          title={brokerErrorTitle(state.sandboxHealth.code, state.sandboxHealth.installed)}
          description={(
            <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {state.sandboxHealth.detail}
            </div>
          )}
          style={{ marginBottom: 16 }}
        />
      ) : null}

      <Form.Item label="网络权限">
        <Select
          aria-label="沙箱网络权限"
          value={config.network_mode}
          options={[
            { value: "no_network", label: "禁止网络" },
            { value: "restricted_network", label: "仅白名单" },
            { value: "full_network", label: "完整网络" },
          ]}
          onChange={(network_mode) => state.updateSettings({
            sandbox_config: { ...config, network_mode },
          })}
        />
      </Form.Item>

      <Typography.Title level={5}>网络白名单</Typography.Title>
      <Typography.Paragraph type="secondary">白名单始终保留，仅在“仅白名单”网络模式下生效。</Typography.Paragraph>
      {config.network_allowlist.length === 0 ? (
        <Typography.Paragraph type="secondary">暂无白名单规则</Typography.Paragraph>
      ) : (
        <Space orientation="vertical" style={{ width: "100%", marginBottom: 12 }}>
          {config.network_allowlist.map((rule, index) => (
            <Space key={index} wrap>
              <Input
                aria-label={`白名单主机 ${index + 1}`}
                placeholder="example.com"
                value={rule.host}
                onChange={(event) => updateAllowlist(index, { host: event.target.value })}
              />
              <InputNumber
                aria-label={`白名单端口 ${index + 1}`}
                min={1}
                max={65535}
                precision={0}
                value={rule.port ?? null}
                placeholder="全部端口"
                onChange={(port) => {
                  if (port === null) updateAllowlist(index, { port: undefined });
                  else if (typeof port === "number" && Number.isInteger(port)) updateAllowlist(index, { port });
                }}
              />
              <Button
                type="text"
                danger
                aria-label={`删除白名单规则 ${index + 1}`}
                icon={<DeleteOutlined />}
                onClick={() => state.updateSettings({
                  sandbox_config: {
                    ...config,
                    network_allowlist: config.network_allowlist.filter((_, ruleIndex) => ruleIndex !== index),
                  },
                })}
              />
            </Space>
          ))}
        </Space>
      )}
      <Button
        icon={<PlusOutlined />}
        disabled={config.network_allowlist.length >= 64}
        onClick={() => state.updateSettings({
          sandbox_config: {
            ...config,
            network_allowlist: [...config.network_allowlist, { host: "" }],
          },
        })}
      >
        添加白名单规则
      </Button>

      <Typography.Title level={5} style={{ marginTop: 24 }}>资源限制</Typography.Title>
      {limitFields.map((field) => (
        <Form.Item key={field.key} label={field.label} extra={field.hint}>
          <InputNumber
            aria-label={field.label}
            min={field.min}
            max={field.max}
            precision={0}
            value={config.limits[field.key]}
            onChange={(value) => {
              if (typeof value !== "number" || !Number.isInteger(value)) return;
              state.updateSettings({
                sandbox_config: { ...config, limits: { ...config.limits, [field.key]: value } },
              });
            }}
          />
        </Form.Item>
      ))}
    </Form>
  );
}
