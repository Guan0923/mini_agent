import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  List,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import { DeleteOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  cancelMemoryJob,
  clearMemories,
  consolidateMemory,
  deleteMemory,
  extractMemory,
  getSettings,
  listMemoryEvidence,
  listMemoryInjectionHistory,
  listMemoryItems,
  listMemoryJobs,
  listSessions,
  restoreMemory,
  setMemoryEnabled,
  updateMemoryConfig,
  type MemoryConfig,
  type MemoryEvidence,
  type MemoryInjectionRecord,
  type MemoryItem,
  type MemoryJob,
  type SessionInfo,
} from "../api";

const KIND_LABEL = { episodic: "Episodic", semantic: "Semantic", procedural: "Procedural" };
const STATUS_COLOR: Record<string, string> = {
  active: "success",
  disabled: "default",
  deleted: "error",
  superseded: "warning",
  pending: "processing",
  running: "processing",
  succeeded: "success",
  failed: "error",
  cancelled: "default",
};

export default function MemoryPage() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [config, setConfig] = useState<MemoryConfig | null>(null);
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [injections, setInjections] = useState<MemoryInjectionRecord[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [evidence, setEvidence] = useState<MemoryEvidence[]>([]);
  const [evidenceItem, setEvidenceItem] = useState<MemoryItem | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [clearText, setClearText] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [settings, memoryItems, memoryJobs, sessionItems, records] = await Promise.all([
        getSettings(),
        listMemoryItems(),
        listMemoryJobs(),
        listSessions("all"),
        listMemoryInjectionHistory(),
      ]);
      setConfig(settings.memory_config);
      setItems(memoryItems);
      setJobs(memoryJobs.slice().reverse());
      setSessions(sessionItems.filter((value) => !value.deleted_at));
      setInjections(records);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const projectIds = useMemo(
    () => Array.from(new Set(items.map((item) => item.project_id).filter((value): value is string => Boolean(value)))),
    [items],
  );

  async function saveConfig(next: MemoryConfig) {
    setSaving(true);
    setError(null);
    try {
      setConfig(await updateMemoryConfig(next));
      setNotice("Memory 设置已保存。");
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setSaving(false);
    }
  }

  async function runAction(action: () => Promise<unknown>, success: string) {
    setSaving(true);
    setError(null);
    try {
      await action();
      setNotice(success);
      await refresh();
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setSaving(false);
    }
  }

  async function showEvidence(item: MemoryItem) {
    setError(null);
    try {
      setEvidence(await listMemoryEvidence(item.memory_id));
      setEvidenceItem(item);
    } catch (value) {
      setError(value instanceof Error ? value.message : String(value));
    }
  }

  if (loading && config === null) return <div style={{ padding: 32 }}><Spin description="正在加载 Memory…" /></div>;

  return (
    <div className="memory-page" style={{ height: "100%", overflow: "auto", padding: 24 }}>
      <Space orientation="vertical" size="large" style={{ width: "100%", maxWidth: 1180, margin: "0 auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 16, alignItems: "center" }}>
          <div>
            <Typography.Title level={2} style={{ margin: 0 }}>Memory</Typography.Title>
            <Typography.Text type="secondary">管理自动提取、长期记忆、证据和实际注入记录。</Typography.Text>
          </div>
          <Button icon={<ReloadOutlined />} onClick={() => void refresh()} loading={loading}>刷新</Button>
        </div>
        {error ? <Alert type="error" showIcon title={error} closable onClose={() => setError(null)} /> : null}
        {notice ? <Alert type="success" showIcon title={notice} closable onClose={() => setNotice(null)} /> : null}

        <Card title="开关与自动化">
          {config ? (
            <Row gutter={[24, 16]}>
              <Col xs={24} md={8}>
                <Space orientation="vertical">
                  <Space><Switch checked={config.generate_memories} loading={saving} onChange={(checked) => void saveConfig({ ...config, generate_memories: checked, automatic_memory_enabled: checked ? config.automatic_memory_enabled : false })} /><Typography.Text strong>1. 允许生成 Memory</Typography.Text></Space>
                  <Typography.Text type="secondary">允许手动提取和整合；关闭时会取消该用户的生成任务。</Typography.Text>
                </Space>
              </Col>
              <Col xs={24} md={8}>
                <Space orientation="vertical">
                  <Space><Switch checked={config.use_memories} loading={saving} onChange={(checked) => void saveConfig({ ...config, use_memories: checked })} /><Typography.Text strong>2. 读取并注入 Memory</Typography.Text></Space>
                  <Typography.Text type="secondary">关闭后数据仍保留，任何模型 Prompt 都不会因 Memory 改变。</Typography.Text>
                </Space>
              </Col>
              <Col xs={24} md={8}>
                <Space orientation="vertical">
                  <Space><Switch checked={config.automatic_memory_enabled} disabled={!config.generate_memories} loading={saving} onChange={(checked) => void saveConfig({ ...config, automatic_memory_enabled: checked })} /><Typography.Text strong>3. 后台自动调度</Typography.Text></Space>
                  <Typography.Text type="secondary">生成开启后才可用；空闲会话自动排队，失败不影响聊天。</Typography.Text>
                </Space>
              </Col>
            </Row>
          ) : null}
        </Card>

        <Card title="手动任务">
          <Space wrap>
            <Select
              showSearch
              style={{ minWidth: 300 }}
              placeholder="选择要提取的会话"
              value={sessionId}
              onChange={setSessionId}
              optionFilterProp="label"
              options={sessions.map((value) => ({ value: value.session_id, label: value.title || value.session_id }))}
            />
            <Button type="primary" disabled={!sessionId || !config?.generate_memories} loading={saving} onClick={() => sessionId && void runAction(() => extractMemory(sessionId), "Phase 1 已排队。")}>手动提取</Button>
            <Button disabled={!config?.generate_memories} loading={saving} onClick={() => void runAction(() => consolidateMemory(null), "全局 Phase 2 已排队。")}>整合全局 Memory</Button>
            {projectIds.map((projectId) => <Button key={projectId} disabled={!config?.generate_memories} onClick={() => void runAction(() => consolidateMemory(projectId), `项目 ${projectId} 的 Phase 2 已排队。`)}>整合项目 {projectId}</Button>)}
          </Space>
        </Card>

        <Card title={`Memory 条目（${items.length}）`}>
          <List
            dataSource={items}
            locale={{ emptyText: <Empty description="暂无 Memory" /> }}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button key="evidence" type="link" onClick={() => void showEvidence(item)}>证据</Button>,
                  item.status === "active" ? <Button key="disable" type="link" onClick={() => void runAction(() => setMemoryEnabled(item.memory_id, false), "Memory 已禁用。")}>禁用</Button> : null,
                  item.status === "disabled" ? <Button key="enable" type="link" onClick={() => void runAction(() => setMemoryEnabled(item.memory_id, true), "Memory 已启用。")}>启用</Button> : null,
                  item.status === "deleted" ? <Button key="restore" type="link" onClick={() => void runAction(() => restoreMemory(item.memory_id), "Memory 已恢复。")}>恢复</Button> : null,
                  item.status !== "deleted" ? <Button key="delete" type="link" danger onClick={() => void runAction(() => deleteMemory(item.memory_id), "Memory 已软删除。")}>删除</Button> : null,
                ].filter(Boolean)}
              >
                <List.Item.Meta
                  title={<Space wrap><Typography.Text strong>{item.title}</Typography.Text><Tag>{KIND_LABEL[item.kind]}</Tag><Tag color={STATUS_COLOR[item.status]}>{item.status}</Tag><Tag>{item.project_id ? `项目 ${item.project_id}` : "全局"}</Tag></Space>}
                  description={<Space orientation="vertical" size={2}><Typography.Paragraph ellipsis={{ rows: 3 }} style={{ margin: 0 }}>{item.content}</Typography.Paragraph><Typography.Text type="secondary">置信度 {item.confidence.toFixed(2)} · 更新于 {new Date(item.updated_at).toLocaleString()}</Typography.Text></Space>}
                />
              </List.Item>
            )}
          />
        </Card>

        <Card title={`任务（${jobs.length}）`}>
          <List
            size="small"
            dataSource={jobs}
            locale={{ emptyText: "暂无任务" }}
            renderItem={(job) => <List.Item actions={job.status === "pending" || job.status === "running" ? [<Button key="cancel" type="link" danger onClick={() => void runAction(() => cancelMemoryJob(job.job_id), "任务已取消。")}>取消</Button>] : []}><Space wrap><Tag>{job.kind}</Tag><Tag color={STATUS_COLOR[job.status]}>{job.status}</Tag><Typography.Text>{job.source_id || "—"}</Typography.Text><Typography.Text type="secondary">尝试 {job.attempts}/{job.max_attempts}</Typography.Text>{job.last_error ? <Typography.Text type="secondary">{job.last_error}</Typography.Text> : null}</Space></List.Item>}
          />
        </Card>

        <Card title="实际注入记录">
          <List
            size="small"
            dataSource={injections}
            locale={{ emptyText: "本进程尚无 Memory 注入记录" }}
            renderItem={(record) => <List.Item><Space orientation="vertical" size={2}><Space><Tag color={record.injected ? "success" : "default"}>{record.injected ? "已注入" : "未注入"}</Tag><Typography.Text>{String(record.session_id || "未知会话")}</Typography.Text><Typography.Text type="secondary">{String(record.operation || "普通请求")}</Typography.Text></Space><Typography.Text code>{JSON.stringify(record.selected_ids || [])}</Typography.Text></Space></List.Item>}
          />
        </Card>

        <Card title="危险操作" styles={{ header: { color: "#cf1322" } }}>
          <Space orientation="vertical">
            <Typography.Text>清空会取消活动任务，并删除当前用户的 Memory、Evidence、候选和 watermark。数据库结构保留。</Typography.Text>
            <Button danger icon={<DeleteOutlined />} onClick={() => setClearOpen(true)}>清空全部 Memory</Button>
          </Space>
        </Card>
      </Space>

      <Modal title={`Evidence · ${evidenceItem?.title || ""}`} open={Boolean(evidenceItem)} footer={null} onCancel={() => setEvidenceItem(null)} width={760}>
        <List dataSource={evidence} locale={{ emptyText: "暂无证据" }} renderItem={(value) => <List.Item><Space orientation="vertical"><Typography.Text>{value.excerpt}</Typography.Text><Typography.Text type="secondary">会话 {value.session_id} · {value.source_kind}</Typography.Text></Space></List.Item>} />
      </Modal>

      <Modal
        title="确认清空全部 Memory"
        open={clearOpen}
        okText="永久清空"
        okButtonProps={{ danger: true, disabled: clearText !== "CLEAR ALL MEMORIES", loading: saving }}
        onCancel={() => { setClearOpen(false); setClearText(""); }}
        onOk={() => void runAction(() => clearMemories(clearText), "Memory 已清空。").then(() => { setClearOpen(false); setClearText(""); })}
      >
        <Typography.Paragraph>请输入 <Typography.Text code>CLEAR ALL MEMORIES</Typography.Text> 以确认。</Typography.Paragraph>
        <Input value={clearText} onChange={(event) => setClearText(event.target.value)} autoComplete="off" />
      </Modal>
    </div>
  );
}
