import { App as AntApp } from "antd";
import { useEffect, useRef, useState } from "react";
import { patchRuntimeConfig, type ProviderConfig } from "../../api";
import { DEFAULT_RUNTIME_NODE_MODEL, isRuntimeTurnNode, normalizeRuntimeNodeModel } from "../../app/runtime/runtimeNodeNormalization";
import type {
  ChatMode,
  Conversation,
  PermissionMode,
  ReasoningEffort,
  RuntimeNodeModel,
  RuntimeStateNode,
  RuntimeTreeNode,
} from "../../types";
import type { SettingsSelectKey } from "./Composer";

interface RuntimeControlsOptions {
  conversation: Conversation | null;
  activeRuntimeNode?: RuntimeStateNode;
  busy: boolean;
  providerConfig?: ProviderConfig | null;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  onFailure: (error: unknown) => void;
}

function nearestUsage(
  node: RuntimeStateNode | undefined,
  nodes: RuntimeTreeNode[],
): { total: number; context: number } | undefined {
  if (!node) return undefined;
  const byKey = new Map(nodes.map((item) => [`${item.session_id}:${item.id}`, item] as const));
  let current: RuntimeStateNode | undefined = node;
  const seen = new Set<string>();
  while (current && !seen.has(`${current.session_id}:${current.id}`)) {
    seen.add(`${current.session_id}:${current.id}`);
    const total = current.usage?.total_tokens;
    const context = current.model?.context_length;
    if (typeof total === "number" && typeof context === "number" && context > 0) return { total, context };
    const parent: RuntimeTreeNode | undefined = current.parent_id
      ? byKey.get(`${current.parent_session_id}:${current.parent_id}`)
      : undefined;
    current = parent && isRuntimeTurnNode(parent) ? parent : undefined;
  }
  return undefined;
}

export function useRuntimeControls({
  conversation,
  activeRuntimeNode,
  busy,
  providerConfig,
  mode,
  onModeChange,
  onFailure,
}: RuntimeControlsOptions) {
  const { modal } = AntApp.useApp();
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("read_only");
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>("medium");
  const [providerName, setProviderName] = useState("unknown");
  const [runtimeModel, setRuntimeModel] = useState<RuntimeNodeModel>(DEFAULT_RUNTIME_NODE_MODEL);
  const [runtimeConfigPending, setRuntimeConfigPending] = useState<Record<SettingsSelectKey, boolean>>({
    mode: false,
    permission: false,
    reasoning: false,
  });
  const runtimeConfigPendingRef = useRef(runtimeConfigPending);
  const [openSettingsSelect, setOpenSettingsSelect] = useState<SettingsSelectKey | null>(null);
  const fullAccessConfirmRef = useRef<{ destroy: () => void } | null>(null);
  const canPatchRuntimeConfig = activeRuntimeNode?.status === "running";

  useEffect(() => {
    const node = activeRuntimeNode;
    if (!node) return;
    const model = normalizeRuntimeNodeModel(node.model);
    setProviderName(node.provider_name || "unknown");
    setRuntimeModel((current) => (
      runtimeConfigPendingRef.current.reasoning ? { ...model, reasoning_effort: current.reasoning_effort } : model
    ));
    if (!runtimeConfigPendingRef.current.permission) setPermissionMode(node.permission_mode || "read_only");
    if (!runtimeConfigPendingRef.current.reasoning) setReasoningEffort(model.reasoning_effort);
    if (!runtimeConfigPendingRef.current.mode && node.running_mode && node.running_mode !== mode) onModeChange(node.running_mode);
  }, [activeRuntimeNode?.id, activeRuntimeNode?.provider_name, activeRuntimeNode?.model, activeRuntimeNode?.permission_mode, activeRuntimeNode?.running_mode]);

  const activeUsage = nearestUsage(activeRuntimeNode, conversation?.runtimeNodes ?? []);
  const usagePercent = activeUsage
    ? Math.max(0, Math.min(100, (activeUsage.total / activeUsage.context) * 100))
    : 0;
  const configuredProviderName = providerConfig?.provider_name?.trim();
  const requestProviderName = configuredProviderName || (providerName && providerName !== "unknown" ? providerName : undefined);
  const requestModel = providerConfig?.model
    ? {
        ...runtimeModel,
        reasoning_effort: reasoningEffort,
        current_model: providerConfig.model,
        context_length: providerConfig.context_size,
        output_length: providerConfig.max_tokens,
      }
    : runtimeModel.current_model && runtimeModel.current_model !== "unknown"
      ? { ...runtimeModel, reasoning_effort: reasoningEffort }
      : undefined;

  async function updateRuntimeConfig(patch: {
    provider_name?: string;
    model?: Partial<RuntimeNodeModel>;
    permission_mode?: PermissionMode;
    full_access_acknowledged?: boolean;
    running_mode?: ChatMode;
  }): Promise<RuntimeStateNode> {
    if (!conversation?.sessionId || !activeRuntimeNode || activeRuntimeNode.status !== "running") {
      throw new Error("当前没有可更新的 running Turn。");
    }
    return patchRuntimeConfig(conversation.sessionId, {
      node_id: activeRuntimeNode.id,
      provider_name: patch.provider_name,
      model: patch.model,
      permission_mode: patch.permission_mode,
      full_access_acknowledged: patch.full_access_acknowledged,
      running_mode: patch.running_mode,
    });
  }

  const activeRuntimeModel = normalizeRuntimeNodeModel(activeRuntimeNode?.model);
  useEffect(() => {
    if (!busy || !canPatchRuntimeConfig || !activeRuntimeNode || !configuredProviderName || !requestModel) return;
    if (
      activeRuntimeNode.provider_name === configuredProviderName
      && activeRuntimeModel.current_model === requestModel.current_model
      && activeRuntimeModel.context_length === requestModel.context_length
      && activeRuntimeModel.output_length === requestModel.output_length
    ) return;
    void updateRuntimeConfig({ provider_name: configuredProviderName, model: requestModel }).catch(onFailure);
  }, [
    busy,
    canPatchRuntimeConfig,
    activeRuntimeNode?.id,
    activeRuntimeNode?.status,
    activeRuntimeNode?.provider_name,
    activeRuntimeModel.current_model,
    activeRuntimeModel.context_length,
    activeRuntimeModel.output_length,
    configuredProviderName,
    requestModel?.current_model,
    requestModel?.context_length,
    requestModel?.output_length,
  ]);

  function setRuntimeConfigFieldPending(field: SettingsSelectKey, pending: boolean) {
    runtimeConfigPendingRef.current = { ...runtimeConfigPendingRef.current, [field]: pending };
    setRuntimeConfigPending(runtimeConfigPendingRef.current);
  }

  async function changeRunningMode(value: ChatMode) {
    const previous = mode;
    if (canPatchRuntimeConfig) setRuntimeConfigFieldPending("mode", true);
    onModeChange(value);
    setOpenSettingsSelect(null);
    if (!canPatchRuntimeConfig) return;
    try {
      const updated = await updateRuntimeConfig({ running_mode: value });
      onModeChange(updated.running_mode);
    } catch (error) {
      onModeChange(previous);
      onFailure(error);
    } finally {
      setRuntimeConfigFieldPending("mode", false);
    }
  }

  async function changePermissionMode(value: PermissionMode) {
    const previous = permissionMode;
    if (value === "full_access" && previous !== "full_access") {
      const confirmed = await new Promise<boolean>((resolve) => {
        fullAccessConfirmRef.current = modal.confirm({
          title: "启用 Full access？",
          content: "这会同时放开文件和网络访问，并标记为非沙箱运行。",
          okText: "继续",
          cancelText: "取消",
          onOk: () => { fullAccessConfirmRef.current = null; resolve(true); },
          onCancel: () => { fullAccessConfirmRef.current = null; resolve(false); },
        });
      });
      if (!confirmed) return;
    }
    if (canPatchRuntimeConfig) setRuntimeConfigFieldPending("permission", true);
    setPermissionMode(value);
    setOpenSettingsSelect(null);
    if (!canPatchRuntimeConfig) return;
    try {
      const updated = await updateRuntimeConfig({
        permission_mode: value,
        full_access_acknowledged: value === "full_access",
      });
      setPermissionMode(updated.permission_mode);
    } catch (error) {
      setPermissionMode(previous);
      onFailure(error);
    } finally {
      setRuntimeConfigFieldPending("permission", false);
    }
  }

  async function changeReasoningEffort(value: ReasoningEffort) {
    const previous = reasoningEffort;
    if (canPatchRuntimeConfig) setRuntimeConfigFieldPending("reasoning", true);
    setReasoningEffort(value);
    setRuntimeModel((current) => ({ ...current, reasoning_effort: value }));
    setOpenSettingsSelect(null);
    if (!canPatchRuntimeConfig) return;
    try {
      const updated = await updateRuntimeConfig({ model: { reasoning_effort: value } });
      const accepted = normalizeRuntimeNodeModel(updated.model).reasoning_effort;
      setReasoningEffort(accepted);
      setRuntimeModel((current) => ({ ...current, reasoning_effort: accepted }));
    } catch (error) {
      setReasoningEffort(previous);
      setRuntimeModel((current) => ({ ...current, reasoning_effort: previous }));
      onFailure(error);
    } finally {
      setRuntimeConfigFieldPending("reasoning", false);
    }
  }

  useEffect(() => () => {
    fullAccessConfirmRef.current?.destroy();
    fullAccessConfirmRef.current = null;
  }, []);

  return {
    permissionMode,
    reasoningEffort,
    runtimeConfigPending,
    openSettingsSelect,
    setOpenSettingsSelect,
    activeUsage,
    usagePercent,
    requestProviderName,
    requestModel,
    changeRunningMode,
    changePermissionMode,
    changeReasoningEffort,
  };
}
