import { Button, Menu, Modal, Space, Spin, Typography } from "antd";
import {
  AgentSettingsSection,
  ProfileSettingsSection,
  RuntimeSettingsSection,
} from "./settings/GeneralSettingsSections";
import { ProviderAddSection, ProviderManageSection } from "./settings/ProviderSettingsSections";
import { SandboxSettingsSection } from "./settings/SandboxSettingsSection";
import type { SettingsSection, UserSettingsModalProps } from "./settings/contracts";
import { useUserSettingsState } from "./settings/useUserSettingsState";

const menuItems = [
  { key: "profile", label: "个人简介" },
  { key: "agent", label: "Agent 配置" },
  { key: "runtime", label: "运行配置" },
  { key: "sandbox", label: "沙箱" },
  { key: "provider_add", label: "添加提供商" },
  { key: "provider_manage", label: "Provider 与模型" },
];

export default function UserSettingsModal(props: UserSettingsModalProps) {
  const state = useUserSettingsState(props);
  const body = state.loading || !state.settings ? (
    <div className="user-settings-loading"><Spin /></div>
  ) : (
    <div className="user-settings-layout">
      <nav className="user-settings-nav" aria-label="设置目录">
        <Menu
          mode="inline"
          selectedKeys={[state.section]}
          items={menuItems}
          onClick={({ key }) => state.setSection(key as SettingsSection)}
        />
      </nav>
      <section className="user-settings-detail">
        {state.section === "profile" ? <ProfileSettingsSection state={state} /> : null}
        {state.section === "agent" ? <AgentSettingsSection state={state} /> : null}
        {state.section === "runtime" ? <RuntimeSettingsSection state={state} /> : null}
        {state.section === "sandbox" ? <SandboxSettingsSection state={state} /> : null}
        {state.section === "provider_add" ? <ProviderAddSection state={state} /> : null}
        {state.section === "provider_manage" ? <ProviderManageSection state={state} /> : null}
        {state.error ? <Typography.Text type="danger">{state.error}</Typography.Text> : null}
      </section>
    </div>
  );

  return (
    <Modal
      className="user-settings-modal"
      title="用户设置"
      open={props.open}
      width={900}
      centered
      mask={{ closable: true }}
      keyboard={false}
      onCancel={state.requestClose}
      footer={state.section === "provider_manage" ? null : (
        <Space>
          <Button
            type="primary"
            aria-label="保存"
            loading={state.saving}
            disabled={state.loading || !state.settings}
            onClick={() => void state.saveCurrent()}
          >
            保存
          </Button>
        </Space>
      )}
    >
      {body}
    </Modal>
  );
}
