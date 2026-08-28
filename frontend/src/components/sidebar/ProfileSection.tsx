import { UserOutlined } from "@ant-design/icons";
import { Button, Input, Popover, Space, Typography } from "antd";
import { useEffect, useState, type CSSProperties } from "react";
import type { LocalProfile } from "../../types";
import { useHorizontalOverflow } from "./useHorizontalOverflow";

interface ProfilePopoverProps {
  profile: LocalProfile;
  onSave?: (profile: { display_name: string; agent_preferences: string }) => Promise<void>;
}

export function ProfileLabel({ label }: { label: string }) {
  const { viewportRef, textRef, overflow, measure } = useHorizontalOverflow(label);
  const [scrolling, setScrolling] = useState(false);

  useEffect(() => {
    setScrolling(false);
  }, [label]);

  const textStyle = { "--profile-shift": `-${overflow}px` } as CSSProperties;
  return (
    <span
      className={`profile-trigger-label-viewport${scrolling ? " is-scrolling" : ""}`}
      ref={viewportRef}
      onMouseEnter={() => setScrolling(measure() > 1)}
      onMouseLeave={() => setScrolling(false)}
    >
      <span className="profile-trigger-label-text" ref={textRef} style={textStyle}>{label}</span>
    </span>
  );
}

export function ProfilePopover({ profile, onSave }: ProfilePopoverProps) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState({ display_name: "", agent_preferences: "" });
  const label = profile.display_name.trim() || "本地用户";

  useEffect(() => {
    if (!open) return;
    setDraft({
      display_name: profile.display_name,
      agent_preferences: profile.agent_preferences,
    });
    setError("");
  }, [open, profile.display_name, profile.agent_preferences]);

  async function save() {
    if (!onSave) return;
    const displayName = draft.display_name.trim();
    if (!displayName) {
      setError("用户名不能为空。");
      return;
    }
    if (displayName.length > 80) {
      setError("用户名不能超过 80 个字符。");
      return;
    }
    if (draft.agent_preferences.length > 4000) {
      setError("Agent 偏好不能超过 4000 个字符。");
      return;
    }
    setSaving(true);
    setError("");
    try {
      await onSave({ display_name: displayName, agent_preferences: draft.agent_preferences.trim() });
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败，请稍后重试。");
    } finally {
      setSaving(false);
    }
  }

  const content = (
    <div className="profile-popover-content">
      <Input
        aria-label="用户名"
        placeholder="设置一个用户名"
        maxLength={80}
        value={draft.display_name}
        onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))}
      />
      <Input.TextArea
        aria-label="Agent 偏好"
        placeholder="例如：回答简洁，先给结论，再给关键步骤。"
        maxLength={4000}
        autoSize={{ minRows: 4, maxRows: 8 }}
        value={draft.agent_preferences}
        onChange={(event) => setDraft((current) => ({ ...current, agent_preferences: event.target.value }))}
      />
      {error ? <Typography.Text type="danger">{error}</Typography.Text> : null}
      <Space className="profile-popover-actions">
        <Button onClick={() => setOpen(false)} disabled={saving}>取消</Button>
        <Button type="primary" aria-label="保存" onClick={() => void save()} loading={saving}>
          保存
        </Button>
      </Space>
    </div>
  );

  return (
    <Popover
      title="个人简介"
      content={content}
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      placement="topLeft"
    >
      <Button className="profile-trigger" type="text" icon={<UserOutlined />} aria-label={`个人简介：${label}`}>
        <ProfileLabel label={label} />
      </Button>
    </Popover>
  );
}
