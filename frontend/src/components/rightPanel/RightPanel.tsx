import { useEffect, useRef, useState, type ReactNode } from "react";
import { App, Button, Dropdown, Empty, Input, Space, Tabs, Tooltip, Typography, type TabsProps } from "antd";
import { CloseOutlined, CommentOutlined, PlusOutlined, ProductOutlined } from "@ant-design/icons";
import {
  closeRightPanelWindow,
  createPanelTerminal,
  createSideChat,
  getRightPanel,
  renameRightPanelWindow,
  updateRightPanel,
} from "../../api";
import type { RightPanelPayload, RightPanelWindow } from "../../types";
import TerminalPane from "./TerminalPane";

const RIGHT_PANEL_TAB_STYLES: TabsProps["styles"] = {
  body: { height: "100%", minHeight: 0 },
  content: { height: "100%", minHeight: 0 },
};

export interface RightPanelController {
  payload: RightPanelPayload | null;
  loading: boolean;
  createWindow: (kind: "side_chat" | "terminal") => Promise<void>;
  closeWindow: (window: RightPanelWindow) => Promise<void>;
  renameWindow: (window: RightPanelWindow, title: string) => Promise<void>;
  setActive: (windowId: string | null) => void;
  setLayout: (patch: Partial<Pick<RightPanelPayload["state"], "width" | "collapsed" | "active_window_id">>) => void;
}

export function useRightPanel(
  sessionId: string | undefined,
  sourceTurnId: string | undefined,
  onHydrate: (window: RightPanelWindow) => Promise<void>,
  onForget: (windowId: string) => void,
): RightPanelController {
  const [payload, setPayload] = useState<RightPanelPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const hydrateRef = useRef(onHydrate);
  const forgetRef = useRef(onForget);
  hydrateRef.current = onHydrate;
  forgetRef.current = onForget;

  useEffect(() => {
    let active = true;
    if (!sessionId) {
      setPayload(null);
      return () => { active = false; };
    }
    setLoading(true);
    void getRightPanel(sessionId)
      .then((next) => {
        if (!active) return;
        setPayload(next);
        void Promise.all(next.windows.filter((item) => item.kind === "side_chat").map(hydrateRef.current));
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [sessionId]);

  const createWindow = async (kind: "side_chat" | "terminal") => {
    if (!sessionId || !sourceTurnId) throw new Error("当前没有可用 Turn。");
    setLoading(true);
    try {
      if (kind === "side_chat") {
        const created = await createSideChat(sessionId, sourceTurnId);
        const next = await getRightPanel(sessionId);
        setPayload(next);
        await hydrateRef.current(created.window);
      } else {
        await createPanelTerminal(sessionId, sourceTurnId);
        setPayload(await getRightPanel(sessionId));
      }
    } finally {
      setLoading(false);
    }
  };

  const closeWindow = async (window: RightPanelWindow) => {
    if (!sessionId) return;
    setPayload((current) => current ? {
      ...current,
      state: {
        ...current.state,
        active_window_id: current.state.active_window_id === window.id
          ? current.windows.find((item) => item.id !== window.id)?.id ?? null
          : current.state.active_window_id,
      },
      windows: current.windows.filter((item) => item.id !== window.id),
    } : current);
    forgetRef.current(window.id);
    await closeRightPanelWindow(sessionId, window.id);
  };

  const renameWindow = async (window: RightPanelWindow, title: string) => {
    if (!sessionId || !title.trim()) return;
    const updated = await renameRightPanelWindow(sessionId, window.id, title.trim());
    setPayload((current) => current ? {
      ...current,
      windows: current.windows.map((item) => item.id === updated.id ? updated : item),
    } : current);
    if (updated.kind === "side_chat") await hydrateRef.current(updated);
  };

  const setActive = (windowId: string | null) => {
    if (!sessionId) return;
    setPayload((current) => current ? { ...current, state: { ...current.state, active_window_id: windowId } } : current);
    void updateRightPanel(sessionId, { active_window_id: windowId });
  };

  const setLayout = (patch: Partial<Pick<RightPanelPayload["state"], "width" | "collapsed" | "active_window_id">>) => {
    if (!sessionId) return;
    setPayload((current) => current ? { ...current, state: { ...current.state, ...patch } } : current);
    void updateRightPanel(sessionId, patch);
  };

  return { payload, loading, createWindow, closeWindow, renameWindow, setActive, setLayout };
}

const creationItems = (
  create: (kind: "side_chat" | "terminal") => void,
  sourceAvailable: boolean,
  terminalAvailable: boolean,
  terminalReason: string,
) => [
  {
    key: "side_chat",
    icon: <CommentOutlined />,
    label: sourceAvailable ? "侧边聊天" : "侧边聊天（当前没有可用 Turn）",
    disabled: !sourceAvailable,
    onClick: () => create("side_chat"),
  },
  {
    key: "terminal",
    icon: <ProductOutlined />,
    label: terminalAvailable ? "终端" : `终端（${terminalReason}）`,
    disabled: !terminalAvailable,
    onClick: () => create("terminal"),
  },
];

interface RightPanelAvailability {
  sourceAvailable: boolean;
  terminalAvailable: boolean;
  terminalReason: string;
}

export function RightPanelLauncher({
  controller,
}: { controller: RightPanelController }) {
  return (
    <Button
      className="right-panel-launcher"
      icon={<PlusOutlined />}
      aria-label="打开右侧边栏"
      onClick={() => controller.setLayout({ collapsed: false })}
    />
  );
}

interface RightPanelProps {
  controller: RightPanelController;
  renderSideChat: (window: RightPanelWindow) => ReactNode;
}

export default function RightPanel({
  controller,
  sourceAvailable,
  terminalAvailable,
  terminalReason,
  renderSideChat,
}: RightPanelProps & RightPanelAvailability) {
  const { message } = App.useApp();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const payload = controller.payload;
  const run = (kind: "side_chat" | "terminal") => void controller.createWindow(kind).catch((error) => {
    void message.error(String((error as Error).message ?? error));
  });
  const saveTitle = (window: RightPanelWindow) => {
    setEditingId(null);
    void controller.renameWindow(window, titleDraft).catch((error) => {
      void message.error(String((error as Error).message ?? error));
    });
  };
  const tabs = (payload?.windows ?? []).map((window) => ({
    key: window.id,
    forceRender: true,
    label: editingId === window.id ? (
      <Input
        autoFocus
        size="small"
        value={titleDraft}
        onChange={(event) => setTitleDraft(event.target.value)}
        onBlur={() => saveTitle(window)}
        onPressEnter={() => saveTitle(window)}
        onClick={(event) => event.stopPropagation()}
      />
    ) : (
      <Tooltip title="双击重命名">
        <span onDoubleClick={() => { setEditingId(window.id); setTitleDraft(window.title); }}>{window.title}</span>
      </Tooltip>
    ),
    children: window.kind === "terminal" ? <TerminalPane panelWindow={window} /> : renderSideChat(window),
  }));
  const extra = (
    <Space size={4}>
      <Dropdown menu={{ items: creationItems(run, sourceAvailable, terminalAvailable, terminalReason) }} trigger={["click"]}>
        <Button type="text" size="small" icon={<PlusOutlined />} aria-label="新增右栏窗口" />
      </Dropdown>
      <Button type="text" size="small" icon={<CloseOutlined />} aria-label="收起右侧边栏" onClick={() => controller.setLayout({ collapsed: true })} />
    </Space>
  );
  if (tabs.length === 0) {
    return (
      <div className="right-panel-empty">
        <Empty description="选择要打开的窗口" />
        <Space>
          <Button icon={<CommentOutlined />} disabled={!sourceAvailable} loading={controller.loading} onClick={() => run("side_chat")}>创建侧边聊天</Button>
          <Button icon={<ProductOutlined />} disabled={!terminalAvailable} loading={controller.loading} onClick={() => run("terminal")}>打开终端</Button>
        </Space>
        {!sourceAvailable ? <Typography.Text type="secondary">当前主聊天没有可用 Turn，暂时不能创建右栏窗口。</Typography.Text> : null}
        {sourceAvailable && !terminalAvailable ? <Typography.Text type="secondary">{terminalReason}</Typography.Text> : null}
        {extra}
      </div>
    );
  }
  return (
    <Tabs
      className="right-panel-tabs"
      type="editable-card"
      hideAdd
      destroyOnHidden={false}
      styles={RIGHT_PANEL_TAB_STYLES}
      activeKey={payload?.state.active_window_id ?? tabs[0]?.key}
      items={tabs}
      tabBarExtraContent={extra}
      onChange={controller.setActive}
      onEdit={(target, action) => {
        if (action !== "remove") return;
        const window = payload?.windows.find((item) => item.id === target);
        if (window) void controller.closeWindow(window).catch((error) => {
          void message.error(String((error as Error).message ?? error));
        });
      }}
    />
  );
}
