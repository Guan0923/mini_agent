import {
  App as AntApp,
  Alert,
  Button,
  Descriptions,
  Empty,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Tree,
  Typography,
  Upload,
  type TreeDataNode,
  type UploadFile,
  type UploadProps,
} from "antd";
import {
  DeleteOutlined,
  FilePdfOutlined,
  FolderOpenOutlined,
  RedoOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";
import {
  deleteRagDocument,
  getRagTree,
  reindexRagDocument,
  uploadRagDocument,
  type RagTreeDocument,
  type RagTreeSection,
} from "../api";

interface KnowledgeBaseContentProps {
  activeSessionId?: string;
}

const STATUS: Record<RagTreeDocument["status"], { label: string; color: string }> = {
  queued: { label: "等待索引", color: "default" },
  indexing: { label: "索引中", color: "processing" },
  ready: { label: "可用", color: "success" },
  not_imported: { label: "未按当前模型索引", color: "warning" },
  stale: { label: "需要重建", color: "warning" },
  failed: { label: "失败", color: "error" },
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function sectionLabel(section: RagTreeSection["section"]): string {
  return `${section.section_type === "project" ? "项目" : "会话"} · ${section.display_name}`;
}

export default function KnowledgeBaseContent({ activeSessionId }: KnowledgeBaseContentProps) {
  const { message, modal } = AntApp.useApp();
  const [tree, setTree] = useState<RagTreeSection[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [actionDocumentId, setActionDocumentId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadSectionId, setUploadSectionId] = useState<string>();
  const [uploadFiles, setUploadFiles] = useState<UploadFile[]>([]);

  async function refresh(showSpinner = false): Promise<void> {
    if (showSpinner) setRefreshing(true);
    setError("");
    try {
      const value = await getRagTree();
      setTree(value);
      setSelectedDocumentId((current) => (
        current && value.some((group) => group.documents.some((document) => document.document_id === current))
          ? current
          : null
      ));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "知识库内容加载失败。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const selectedDocument = useMemo(
    () => tree.flatMap((group) => group.documents).find((item) => item.document_id === selectedDocumentId) ?? null,
    [selectedDocumentId, tree],
  );

  const treeData = useMemo<TreeDataNode[]>(() => tree.map((group) => ({
    key: `section:${group.section.section_id}`,
    icon: <FolderOpenOutlined />,
    title: (
      <span className="rag-tree-section-title">
        <span>{group.section.display_name}</span>
        <Tag>{group.section.section_type === "project" ? "项目" : "会话"}</Tag>
      </span>
    ),
    selectable: false,
    children: group.documents.map((document) => ({
      key: `document:${document.document_id}`,
      icon: <FilePdfOutlined />,
      isLeaf: true,
      title: (
        <span className="rag-tree-document-title">
          <span className="rag-tree-document-name" title={document.filename}>{document.filename}</span>
          <Tag color={STATUS[document.status].color}>{STATUS[document.status].label}</Tag>
        </span>
      ),
    })),
  })), [tree]);

  function openUpload(): void {
    const preferred = tree.find((group) => group.section.session_id === activeSessionId)?.section.section_id
      ?? tree[0]?.section.section_id;
    setUploadSectionId(preferred);
    setUploadFiles([]);
    setUploadOpen(true);
  }

  const uploadProps: UploadProps = {
    accept: ".pdf,application/pdf",
    maxCount: 1,
    fileList: uploadFiles,
    beforeUpload: (file) => {
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        message.error("知识库只支持 PDF 文件");
        return Upload.LIST_IGNORE;
      }
      setUploadFiles([file]);
      return false;
    },
    onRemove: () => {
      setUploadFiles([]);
      return true;
    },
  };

  async function submitUpload(): Promise<void> {
    const selected = uploadFiles[0];
    const file = (selected?.originFileObj ?? selected) as File | undefined;
    if (!uploadSectionId || !file) return;
    setUploading(true);
    setError("");
    try {
      await uploadRagDocument(uploadSectionId, file);
      setUploadOpen(false);
      setUploadFiles([]);
      message.success("PDF 已加入知识库并等待索引");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "PDF 导入失败。");
    } finally {
      setUploading(false);
    }
  }

  async function reindex(document: RagTreeDocument): Promise<void> {
    setActionDocumentId(document.document_id);
    setError("");
    try {
      await reindexRagDocument(document.document_id);
      message.success("已提交重新索引任务");
      await refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "重新索引失败。");
    } finally {
      setActionDocumentId(null);
    }
  }

  function confirmDelete(document: RagTreeDocument): void {
    modal.confirm({
      title: `删除知识库文件 ${document.filename}？`,
      content: "只会删除知识库副本和索引，不会删除原始项目文件或会话附件。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setActionDocumentId(document.document_id);
        setError("");
        try {
          const result = await deleteRagDocument(document.document_id);
          if (result.warning) message.warning("文件已删除，但部分向量清理失败");
          else message.success("知识库文件已删除");
          setSelectedDocumentId(null);
          await refresh();
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "删除知识库文件失败。");
          throw cause;
        } finally {
          setActionDocumentId(null);
        }
      },
    });
  }

  const busy = selectedDocument?.status === "queued" || selectedDocument?.status === "indexing";
  const details = selectedDocument ? [
    { key: "name", label: "文件名", children: selectedDocument.filename },
    { key: "source", label: "来源", children: selectedDocument.source === "knowledge_base" ? "知识库上传" : selectedDocument.source },
    { key: "size", label: "大小", children: formatBytes(selectedDocument.size_bytes) },
    { key: "created", label: "导入时间", children: new Date(selectedDocument.created_at * 1000).toLocaleString() },
    { key: "path", label: "管理路径", children: selectedDocument.relative_path, span: "filled" as const },
    { key: "status", label: "索引状态", children: <Tag color={STATUS[selectedDocument.status].color}>{STATUS[selectedDocument.status].label}</Tag> },
    {
      key: "error",
      label: "错误信息",
      children: selectedDocument.ingestion_error || selectedDocument.error || "无",
      span: "filled" as const,
    },
  ] : [];

  return (
    <div className="rag-content">
      <div className="rag-content-header">
        <div>
          <Typography.Title level={4}>知识库内容</Typography.Title>
          <Typography.Text type="secondary">按项目和会话分区管理已导入的 PDF。</Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void refresh(true)} aria-label="刷新知识库">刷新</Button>
          <Button
            type="primary"
            icon={<UploadOutlined />}
            disabled={tree.length === 0}
            onClick={openUpload}
            aria-label="导入 PDF"
          >导入 PDF</Button>
        </Space>
      </div>
      {error ? <Alert type="error" showIcon title={error} /> : null}
      {loading ? <div className="rag-content-loading"><Spin /></div> : tree.length === 0 ? (
        <Empty description="暂无知识库分区，请先创建或打开一个会话。" />
      ) : (
        <div className="rag-content-browser">
          <div className="rag-tree-pane">
            <Tree
              className="rag-tree"
              blockNode
              showIcon
              showLine={{ showLeafIcon: true }}
              defaultExpandedKeys={tree.map((group) => `section:${group.section.section_id}`)}
              selectedKeys={selectedDocumentId ? [`document:${selectedDocumentId}`] : []}
              treeData={treeData}
              onSelect={(keys) => {
                const key = String(keys[0] ?? "");
                setSelectedDocumentId(key.startsWith("document:") ? key.slice("document:".length) : null);
              }}
            />
          </div>
          <div className="rag-document-details">
            {selectedDocument ? (
              <>
                <div className="rag-document-actions">
                  <Typography.Title level={5}>{selectedDocument.filename}</Typography.Title>
                  <Space wrap>
                    <Button
                      icon={<RedoOutlined />}
                      disabled={busy}
                      loading={actionDocumentId === selectedDocument.document_id}
                      onClick={() => void reindex(selectedDocument)}
                      aria-label="重新索引"
                    >重新索引</Button>
                    <Button
                      danger
                      icon={<DeleteOutlined />}
                      disabled={busy}
                      onClick={() => confirmDelete(selectedDocument)}
                      aria-label="删除"
                    >删除</Button>
                  </Space>
                </div>
                {busy ? <Alert type="info" showIcon title="索引任务运行期间不能删除或重复提交。" /> : null}
                <Descriptions bordered size="small" column={1} items={details} />
              </>
            ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一个 PDF 查看详情" />}
          </div>
        </div>
      )}
      <Modal
        title="导入 PDF 到知识库"
        open={uploadOpen}
        okText="开始导入"
        cancelText="取消"
        confirmLoading={uploading}
        okButtonProps={{ disabled: !uploadSectionId || uploadFiles.length === 0 }}
        onOk={() => void submitUpload()}
        onCancel={() => { if (!uploading) setUploadOpen(false); }}
      >
        <div className="rag-upload-form">
          <label htmlFor="rag-upload-section">目标分区</label>
          <Select
            id="rag-upload-section"
            aria-label="目标分区"
            value={uploadSectionId}
            options={tree.map((group) => ({ value: group.section.section_id, label: sectionLabel(group.section) }))}
            onChange={setUploadSectionId}
          />
          <Upload {...uploadProps}>
            <Button icon={<FilePdfOutlined />}>选择 PDF</Button>
          </Upload>
        </div>
      </Modal>
    </div>
  );
}
