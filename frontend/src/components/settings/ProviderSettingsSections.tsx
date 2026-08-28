import { AutoComplete, Button, Collapse, Form, Input, Select, Space, Tag, Typography } from "antd";
import type { UserSettingsState } from "./useUserSettingsState";

type SectionProps = { state: UserSettingsState };

export function ProviderAddSection({ state }: SectionProps) {
  const draft = state.providerAddDraft;
  return (
    <Form layout="vertical">
      <Typography.Title level={4}>添加提供商</Typography.Title>
      <Space.Compact block>
        <Form.Item label="协议" style={{ flex: 1 }}>
          <SelectProtocol
            value={draft.protocol}
            onChange={(protocol) => state.setProviderAddDraft((current) => ({ ...current, protocol }))}
          />
        </Form.Item>
        <Form.Item label="配置名称" style={{ flex: 1 }}>
          <Input value={draft.provider_name} onChange={(event) => state.setProviderAddDraft((current) => ({ ...current, provider_name: event.target.value }))} />
        </Form.Item>
      </Space.Compact>
      <Form.Item label="Base URL">
        <Input value={draft.base_url} onChange={(event) => state.setProviderAddDraft((current) => ({ ...current, base_url: event.target.value }))} />
      </Form.Item>
      <Form.Item label="模型">
        <AutoComplete
          options={state.matchingModels("new", draft.model)}
          value={draft.model}
          onChange={(model) => state.setProviderAddDraft((current) => ({ ...current, model }))}
          placeholder="手动输入或先获取模型列表"
        />
        <Button
          type="link"
          loading={state.modelsLoading.new}
          onClick={() => void state.discoverModels("new", {
            provider_name: draft.provider_name,
            protocol: draft.protocol,
            base_url: draft.base_url,
            api_key: draft.api_key,
          })}
        >获取 /v1/models</Button>
      </Form.Item>
      <Space.Compact block>
        <Form.Item label="最大输出 token" style={{ flex: 1 }}>
          <Input type="number" value={draft.max_tokens} onChange={(event) => state.setProviderAddDraft((current) => ({ ...current, max_tokens: Number(event.target.value) }))} />
        </Form.Item>
        <Form.Item label="上下文大小" style={{ flex: 1 }}>
          <Input type="number" value={draft.context_size} onChange={(event) => state.setProviderAddDraft((current) => ({ ...current, context_size: Number(event.target.value) }))} />
        </Form.Item>
      </Space.Compact>
      <Form.Item label="API Key">
        <Input.Password placeholder="输入 API Key" value={draft.api_key} onChange={(event) => state.setProviderAddDraft((current) => ({ ...current, api_key: event.target.value }))} />
      </Form.Item>
    </Form>
  );
}

function SelectProtocol({
  value,
  onChange,
}: {
  value: "chat_completions" | "responses" | "messages";
  onChange: (value: "chat_completions" | "responses" | "messages") => void;
}) {
  return (
    <Select
      value={value}
      options={[
        { value: "chat_completions", label: "Chat Completions" },
        { value: "responses", label: "Responses" },
        { value: "messages", label: "Messages" },
      ]}
      onChange={onChange}
    />
  );
}

export function ProviderManageSection({ state }: SectionProps) {
  const settings = state.settings!;
  if (settings.provider_configs.length === 0) {
    return (
      <div className="provider-management">
        <Typography.Title level={4}>Provider 与模型</Typography.Title>
        <Typography.Text type="secondary">暂无提供商，请先在“添加提供商”中创建。</Typography.Text>
      </div>
    );
  }

  return (
    <div className="provider-management">
      <Typography.Title level={4}>Provider 与模型</Typography.Title>
      <Collapse
        items={settings.provider_configs.map((provider) => {
          const draft = state.providerDrafts[provider.id] ?? { provider_name: provider.provider_name, model: provider.model, api_key: "" };
          const modelFeedback = state.managedModelFeedback[provider.id];
          return {
            key: provider.id,
            label: (
              <span>
                {provider.provider_name} · {provider.model || "未选择模型"} {provider.is_active ? <Tag color="green">当前使用</Tag> : null}
              </span>
            ),
            children: (
              <Form layout="vertical">
                <Form.Item label="配置名称">
                  <Input value={draft.provider_name} onChange={(event) => state.updateProviderDraft(provider.id, { provider_name: event.target.value })} />
                </Form.Item>
                <Form.Item label="Base URL"><Input value={provider.base_url} disabled /></Form.Item>
                <Form.Item label="模型">
                  <AutoComplete
                    options={state.matchingModels(provider.id, state.managedModelQueries[provider.id] ?? "")}
                    value={draft.model}
                    onChange={(model) => state.updateProviderDraft(provider.id, { model })}
                    onSelect={(model) => {
                      state.updateProviderDraft(provider.id, { model });
                      state.setManagedModelQueries((current) => ({ ...current, [provider.id]: "" }));
                      state.setManagedModelOpen((current) => ({ ...current, [provider.id]: false }));
                    }}
                    open={state.managedModelOpen[provider.id] ?? false}
                    onOpenChange={(nextOpen) => state.setManagedModelOpen((current) => ({ ...current, [provider.id]: nextOpen }))}
                    showSearch={{
                      filterOption: false,
                      onSearch: (query) => state.setManagedModelQueries((current) => ({ ...current, [provider.id]: query })),
                    }}
                    placeholder="手动输入或先获取模型列表"
                  />
                  <Button
                    type="link"
                    loading={state.modelsLoading[provider.id]}
                    onClick={() => void state.discoverModels(provider.id, {
                      provider_name: provider.provider_name,
                      protocol: provider.protocol,
                      base_url: provider.base_url,
                      api_key: draft.api_key,
                    })}
                  >获取 /v1/models</Button>
                  {modelFeedback ? (
                    <Typography.Text
                      aria-live="polite"
                      type={modelFeedback.status === "error" ? "danger" : modelFeedback.status}
                      style={{ display: "block", marginTop: 4 }}
                    >
                      {modelFeedback.message}
                    </Typography.Text>
                  ) : null}
                </Form.Item>
                <Form.Item label="API Key">
                  <Input.Password
                    value={draft.api_key}
                    placeholder={provider.api_key_configured ? "已配置，留空以保持不变" : "输入 API Key"}
                    onChange={(event) => state.updateProviderDraft(provider.id, { api_key: event.target.value })}
                  />
                </Form.Item>
                <Space>
                  <Button type="primary" loading={state.saving} onClick={() => void state.saveManagedProvider(provider)}>保存修改</Button>
                  {!provider.is_active ? <Button onClick={() => void state.activateProvider(provider)}>设为当前使用</Button> : null}
                  <Button danger onClick={() => state.confirmDeleteProvider(provider)}>删除</Button>
                </Space>
              </Form>
            ),
          };
        })}
      />
    </div>
  );
}
