import { DeleteOutlined, MinusCircleOutlined, PlusOutlined } from "@ant-design/icons";
import { Alert, App, Button, Collapse, Form, Input, Segmented, Space, Spin, Switch, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import {
  createMcpServer,
  deleteMcpServer,
  getMcpSettings,
  setMcpEnabled,
  setMcpServerEnabled,
  testMcpServer,
  updateMcpServer,
  type McpServerInput,
  type McpServerSettings,
  type McpSettingsResponse,
} from "../../api";

type EnvironmentRow = { name: string; value: string };
type SecretRow = { name: string; value: string; configured?: boolean; remove?: boolean };
type ServerFormValues = {
  transport: "stdio" | "streamable_http";
  url: string;
  headers: EnvironmentRow[];
  headerSecrets: SecretRow[];
  name?: string;
  command: string;
  args: string[];
  cwd?: string;
  env: EnvironmentRow[];
  secrets: SecretRow[];
};

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error ? cause.message : fallback;
}

function formValues(server?: McpServerSettings): ServerFormValues {
  return {
    transport: server?.transport ?? "stdio",
    url: server?.url ?? "",
    headers: Object.entries(server?.headers ?? {}).map(([name, value]) => ({ name, value })),
    headerSecrets: (server?.secret_headers ?? []).map(({ name, configured }) => ({ name, value: "", configured, remove: false })),
    name: server?.name ?? "",
    command: server?.command ?? "",
    args: server?.args.length ? server.args : [""],
    cwd: server?.cwd ?? "",
    env: Object.entries(server?.env ?? {}).map(([name, value]) => ({ name, value })),
    secrets: (server?.secret_env ?? []).map(({ name, configured }) => ({
      name,
      value: "",
      configured,
      remove: false,
    })),
  };
}

function inputFrom(values: ServerFormValues, enabled: boolean): McpServerInput {
  if (values.transport === "streamable_http") {
    return {
      transport: "streamable_http", url: values.url.trim(),
      command: "", args: [], cwd: null, env: {}, secrets: {}, remove_secrets: [], enabled,
      headers: Object.fromEntries((values.headers ?? []).filter((row) => row?.name?.trim()).map((row) => [row.name.trim(), row.value ?? ""])),
      header_secrets: Object.fromEntries((values.headerSecrets ?? []).filter((row) => row?.name?.trim() && row.value && !row.remove).map((row) => [row.name.trim(), row.value])),
      remove_header_secrets: (values.headerSecrets ?? []).filter((row) => row?.configured && row.remove).map((row) => row.name),
    };
  }
  const environment = Object.fromEntries(
    (values.env ?? [])
      .filter((item) => item?.name?.trim())
      .map((item) => [item.name.trim(), item.value ?? ""]),
  );
  const secrets = Object.fromEntries(
    (values.secrets ?? [])
      .filter((item) => item?.name?.trim() && item.value)
      .map((item) => [item.name.trim(), item.value]),
  );
  const removeSecrets = (values.secrets ?? [])
    .filter((item) => item?.configured && item.remove)
    .map((item) => item.name);
  return {
    transport: "stdio",
    command: values.command.trim(),
    args: (values.args ?? []).filter((item) => item !== ""),
    cwd: values.cwd?.trim() || null,
    env: environment,
    secrets,
    remove_secrets: removeSecrets,
    enabled,
  };
}

function ServerEditor({
  server,
  saving,
  onSave,
}: {
  server?: McpServerSettings;
  saving: boolean;
  onSave: (values: ServerFormValues) => Promise<void>;
}) {
  const [form] = Form.useForm<ServerFormValues>();
  const transport = Form.useWatch("transport", form) ?? server?.transport ?? "stdio";
  const url = Form.useWatch("url", form) ?? "";

  useEffect(() => {
    form.setFieldsValue(formValues(server));
  }, [form, server]);

  return (
    <Form<ServerFormValues>
      form={form}
      layout="vertical"
      initialValues={formValues(server)}
      onFinish={(values) => void onSave(values)}
    >
      {!server ? (
        <Form.Item
          name="name"
          label="Server 名称"
          rules={[
            { required: true, message: "请输入名称" },
            { pattern: /^[A-Za-z0-9_-]+$/, message: "仅支持字母、数字、_ 和 -" },
          ]}
        >
          <Input aria-label="MCP Server 名称" placeholder="例如 filesystem" />
        </Form.Item>
      ) : (
        <Form.Item label="Server 名称">
          <Input value={server.name} disabled />
        </Form.Item>
      )}
      <Form.Item name="transport" label="连接方式">
        <Segmented options={[{ label: "本地命令", value: "stdio" }, { label: "Streamable HTTP", value: "streamable_http" }]} />
      </Form.Item>
      {transport === "stdio" ? <>
      <Form.Item name="command" label="Command" rules={[{ required: true, whitespace: true, message: "请输入命令" }]}>
        <Input aria-label={`MCP Command ${server?.name ?? "new"}`} placeholder="例如 python" />
      </Form.Item>
      <Form.Item label="参数">
        <Form.List name="args">
          {(fields, { add, remove }) => (
            <Space orientation="vertical" style={{ width: "100%" }}>
              {fields.map((field, index) => (
                <Space key={field.key} style={{ width: "100%" }}>
                  <Form.Item name={field.name} noStyle>
                    <Input aria-label={`MCP 参数 ${index + 1}`} placeholder="一个参数" />
                  </Form.Item>
                  <Button type="text" aria-label={`删除 MCP 参数 ${index + 1}`} icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
                </Space>
              ))}
              <Button type="dashed" icon={<PlusOutlined />} onClick={() => add("")}>添加参数</Button>
            </Space>
          )}
        </Form.List>
      </Form.Item>
      <Form.Item name="cwd" label="工作目录（可选）">
        <Input aria-label={`MCP 工作目录 ${server?.name ?? "new"}`} />
      </Form.Item>
      <Typography.Title level={5}>普通环境变量</Typography.Title>
      <Form.List name="env">
        {(fields, { add, remove }) => (
          <Space orientation="vertical" style={{ width: "100%", marginBottom: 16 }}>
            {fields.map((field, index) => (
              <Space key={field.key} style={{ width: "100%" }} align="start">
                <Form.Item name={[field.name, "name"]} rules={[{ required: true, message: "请输入变量名" }]}>
                  <Input aria-label={`MCP 环境变量名 ${index + 1}`} placeholder="NAME" />
                </Form.Item>
                <Form.Item name={[field.name, "value"]}>
                  <Input aria-label={`MCP 环境变量值 ${index + 1}`} placeholder="value" />
                </Form.Item>
                <Button type="text" aria-label={`删除 MCP 环境变量 ${index + 1}`} icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
              </Space>
            ))}
            <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ name: "", value: "" })}>
              添加普通环境变量
            </Button>
          </Space>
        )}
      </Form.List>
      <Typography.Title level={5}>密钥环境变量</Typography.Title>
      <Typography.Paragraph type="secondary">
        密钥只写入系统凭据库，已保存的值不会回填。留空会保留原值，只有“清除”会删除凭据。
      </Typography.Paragraph>
      <Form.List name="secrets">
        {(fields, { add, remove }) => (
          <Space orientation="vertical" style={{ width: "100%", marginBottom: 16 }}>
            {fields.map((field, index) => (
              <Form.Item
                key={field.key}
                noStyle
                shouldUpdate={(previous, current) => (
                  previous.secrets?.[field.name]?.remove !== current.secrets?.[field.name]?.remove
                  || previous.secrets?.[field.name]?.configured !== current.secrets?.[field.name]?.configured
                )}
              >
                {() => {
                  const configured = form.getFieldValue(["secrets", field.name, "configured"]);
                  const markedForRemoval = form.getFieldValue(["secrets", field.name, "remove"]);
                  return (
                    <Space style={{ width: "100%" }} align="start">
                      <Form.Item name={[field.name, "name"]} rules={[{ required: true, message: "请输入变量名" }]}>
                        <Input
                          aria-label={`MCP 密钥变量名 ${index + 1}`}
                          placeholder="API_TOKEN"
                          disabled={configured}
                        />
                      </Form.Item>
                      <Form.Item name={[field.name, "value"]}>
                        <Input.Password
                          aria-label={`MCP 密钥变量值 ${index + 1}`}
                          placeholder={configured ? "留空以保留" : "输入密钥"}
                          disabled={markedForRemoval}
                        />
                      </Form.Item>
                      {configured ? <Tag color={markedForRemoval ? "error" : "success"}>{markedForRemoval ? "待清除" : "已配置"}</Tag> : null}
                      {configured ? (
                        <Button
                          danger={!markedForRemoval}
                          onClick={() => form.setFieldValue(["secrets", field.name, "remove"], !markedForRemoval)}
                        >
                          {markedForRemoval ? "撤销清除" : "清除"}
                        </Button>
                      ) : (
                        <Button type="text" aria-label={`删除 MCP 密钥变量 ${index + 1}`} icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
                      )}
                    </Space>
                  );
                }}
              </Form.Item>
            ))}
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() => add({ name: "", value: "", configured: false, remove: false })}
            >
              添加密钥环境变量
            </Button>
          </Space>
        )}
      </Form.List>
      </> : <>
        <Form.Item name="url" label="MCP URL" rules={[{ required: true, whitespace: true, message: "请输入 HTTP 或 HTTPS 地址" }, { pattern: /^https?:\/\//, message: "仅支持 HTTP 或 HTTPS" }]}>
          <Input aria-label={`MCP URL ${server?.name ?? "new"}`} placeholder="https://example.com/mcp" />
        </Form.Item>
        {url.startsWith("http://") ? <Alert type="warning" showIcon title="HTTP 会明文传输请求头和内容。" style={{ marginBottom: 16 }} /> : null}
        <Typography.Title level={5}>普通请求头</Typography.Title>
        <Form.List name="headers">
          {(fields, { add, remove }) => <Space orientation="vertical" style={{ width: "100%", marginBottom: 16 }}>
            {fields.map((field, index) => <Space key={field.key} wrap align="start">
              <Form.Item name={[field.name, "name"]} rules={[{ required: true, message: "请输入请求头名称" }]}><Input aria-label={`MCP 请求头名称 ${index + 1}`} /></Form.Item>
              <Form.Item name={[field.name, "value"]}><Input aria-label={`MCP 请求头值 ${index + 1}`} /></Form.Item>
              <Button type="text" aria-label={`删除 MCP 请求头 ${index + 1}`} icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
            </Space>)}
            <Button icon={<PlusOutlined />} onClick={() => add({ name: "", value: "" })}>添加请求头</Button>
          </Space>}
        </Form.List>
        <Typography.Title level={5}>密钥请求头</Typography.Title>
        <Form.List name="headerSecrets">
          {(fields, { add, remove }) => <Space orientation="vertical" style={{ width: "100%", marginBottom: 16 }}>
            {fields.map((field, index) => <Form.Item key={field.key} noStyle shouldUpdate>
              {() => {
                const row = form.getFieldValue(["headerSecrets", field.name]) as SecretRow | undefined;
                return <Space wrap align="start">
                  <Form.Item name={[field.name, "name"]} rules={[{ required: true, message: "请输入请求头名称" }]}><Input aria-label={`MCP 密钥请求头名称 ${index + 1}`} placeholder="Authorization" disabled={row?.configured} /></Form.Item>
                  <Form.Item name={[field.name, "value"]}><Input.Password aria-label={`MCP 密钥请求头值 ${index + 1}`} placeholder={row?.configured ? "留空以保留" : "Bearer ..."} disabled={row?.remove} /></Form.Item>
                  {row?.configured ? <><Tag color={row.remove ? "error" : "success"}>{row.remove ? "待清除" : "已配置"}</Tag><Button onClick={() => form.setFieldValue(["headerSecrets", field.name, "remove"], !row.remove)}>{row.remove ? "撤销清除" : "清除"}</Button></> : <Button type="text" aria-label={`删除 MCP 密钥请求头 ${index + 1}`} icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />}
                </Space>;
              }}
            </Form.Item>)}
            <Button icon={<PlusOutlined />} onClick={() => add({ name: "", value: "", configured: false, remove: false })}>添加密钥请求头</Button>
          </Space>}
        </Form.List>
      </>}
      <Button type="primary" htmlType="submit" loading={saving}>
        {server ? "保存 Server" : "创建 Server"}
      </Button>
    </Form>
  );
}

export function McpSettingsSection() {
  const { modal, message } = App.useApp();
  const [data, setData] = useState<McpSettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [globalSaving, setGlobalSaving] = useState(false);
  const [rowSaving, setRowSaving] = useState<Record<string, boolean>>({});
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  async function load() {
    setLoading(true);
    setError("");
    try {
      setData(await getMcpSettings());
    } catch (cause) {
      setError(errorMessage(cause, "MCP 设置加载失败。"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function setRowError(name: string, value: string) {
    setRowErrors((current) => ({ ...current, [name]: value }));
  }

  async function toggleGlobal(enabled: boolean) {
    if (!data) return;
    const previousEnabled = data.enabled;
    setData((current) => current ? { ...current, enabled } : current);
    setGlobalSaving(true);
    try {
      setData(await setMcpEnabled(enabled));
    } catch (cause) {
      setData((current) => current ? { ...current, enabled: previousEnabled } : current);
      setError(errorMessage(cause, "MCP 总开关保存失败。"));
    } finally {
      setGlobalSaving(false);
    }
  }

  async function toggleServer(server: McpServerSettings, enabled: boolean) {
    if (!data) return;
    const previousEnabled = server.enabled;
    setData((current) => current ? {
      ...current,
      servers: current.servers.map((item) => item.name === server.name ? { ...item, enabled } : item),
    } : current);
    setRowSaving((current) => ({ ...current, [server.name]: true }));
    setRowError(server.name, "");
    try {
      const updated = await setMcpServerEnabled(server.name, enabled);
      setData((current) => current ? {
        ...current,
        servers: current.servers.map((item) => item.name === updated.name ? updated : item),
      } : current);
    } catch (cause) {
      setData((current) => current ? {
        ...current,
        servers: current.servers.map((item) => (
          item.name === server.name ? { ...item, enabled: previousEnabled } : item
        )),
      } : current);
      setRowError(server.name, errorMessage(cause, "MCP Server 开关保存失败。"));
    } finally {
      setRowSaving((current) => ({ ...current, [server.name]: false }));
    }
  }

  async function createServer(values: ServerFormValues) {
    const name = values.name?.trim() ?? "";
    setRowSaving((current) => ({ ...current, new: true }));
    setRowError("new", "");
    try {
      await createMcpServer({ name, ...inputFrom(values, true) });
      message.success(`已创建 MCP Server：${name}`);
      await load();
    } catch (cause) {
      setRowError("new", errorMessage(cause, "MCP Server 创建失败。"));
    } finally {
      setRowSaving((current) => ({ ...current, new: false }));
    }
  }

  async function saveServer(server: McpServerSettings, values: ServerFormValues) {
    setRowSaving((current) => ({ ...current, [server.name]: true }));
    setRowError(server.name, "");
    try {
      const updated = await updateMcpServer(server.name, inputFrom(values, server.enabled));
      setData((current) => current ? {
        ...current,
        servers: current.servers.map((item) => item.name === updated.name ? updated : item),
      } : current);
      message.success(`已保存 MCP Server：${server.name}`);
    } catch (cause) {
      setRowError(server.name, errorMessage(cause, "MCP Server 保存失败。"));
    } finally {
      setRowSaving((current) => ({ ...current, [server.name]: false }));
    }
  }

  function confirmDelete(server: McpServerSettings) {
    modal.confirm({
      title: `删除 MCP Server “${server.name}”？`,
      content: "将同时删除该 Server 管理的系统凭据，此操作无法恢复。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setRowSaving((current) => ({ ...current, [server.name]: true }));
        try {
          await deleteMcpServer(server.name);
          setData((current) => current ? {
            ...current,
            servers: current.servers.filter((item) => item.name !== server.name),
          } : current);
          message.success(`已删除 MCP Server：${server.name}`);
        } catch (cause) {
          setRowError(server.name, errorMessage(cause, "MCP Server 删除失败。"));
          throw cause;
        } finally {
          setRowSaving((current) => ({ ...current, [server.name]: false }));
        }
      },
    });
  }

  async function testConnection(server: McpServerSettings) {
    setTesting((current) => ({ ...current, [server.name]: true }));
    setRowError(server.name, "");
    try {
      const result = await testMcpServer(server.name);
      message.success(`连接成功：${result.protocol_version}；工具 ${result.counts.tools}，资源 ${result.counts.resources}，资源模板 ${result.counts.resource_templates}，提示词 ${result.counts.prompts}；能力：${result.capabilities.join("、") || "无"}`);
    } catch (cause) {
      setRowError(server.name, errorMessage(cause, "MCP 连接测试失败。"));
    } finally {
      setTesting((current) => ({ ...current, [server.name]: false }));
    }
  }

  if (loading && !data) {
    return <div className="user-settings-loading"><Spin /></div>;
  }

  const items = [
    {
      key: "new",
      label: "新增 MCP Server",
      children: (
        <>
          {rowErrors.new ? <Alert type="error" showIcon title={rowErrors.new} style={{ marginBottom: 16 }} /> : null}
          <ServerEditor saving={rowSaving.new ?? false} onSave={createServer} />
        </>
      ),
    },
    ...(data?.servers ?? []).map((server) => ({
      key: server.name,
      label: <Space wrap><span>{server.name}</span>{server.enabled ? <Tag color="success">已启用</Tag> : <Tag>已停用</Tag>}</Space>,
      extra: (
        <Switch
          aria-label={`启用 MCP Server ${server.name}`}
          checked={server.enabled}
          loading={rowSaving[server.name]}
          onClick={(_, event) => event.stopPropagation()}
          onChange={(enabled) => void toggleServer(server, enabled)}
        />
      ),
      children: (
        <>
          {rowErrors[server.name] ? <Alert type="error" showIcon title={rowErrors[server.name]} style={{ marginBottom: 16 }} /> : null}
          <ServerEditor
            server={server}
            saving={rowSaving[server.name] ?? false}
            onSave={(values) => saveServer(server, values)}
          />
          <Space style={{ marginTop: 16 }} wrap>
            <Button loading={testing[server.name]} onClick={() => void testConnection(server)}>测试连接</Button>
            <Button danger icon={<DeleteOutlined />} loading={rowSaving[server.name]} onClick={() => confirmDelete(server)}>
              删除 Server
            </Button>
          </Space>
        </>
      ),
    })),
  ];

  return (
    <div>
      <Typography.Title level={4}>MCP</Typography.Title>
      <Typography.Paragraph type="secondary">
        总开关和 Server 开关立即保存，仅影响下一个 Turn。连接测试始终使用已保存配置，不受总开关影响。
      </Typography.Paragraph>
      <Space style={{ marginBottom: 16 }} wrap>
        <Switch
          aria-label="启用 MCP"
          checked={data?.enabled ?? false}
          loading={globalSaving}
          onChange={(enabled) => void toggleGlobal(enabled)}
        />
        <Typography.Text>启用 MCP</Typography.Text>
      </Space>
      {error ? <Alert type="error" showIcon title={error} style={{ marginBottom: 16 }} /> : null}
      <Collapse items={items} />
    </div>
  );
}
