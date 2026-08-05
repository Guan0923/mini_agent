import { Button, Tooltip, type ButtonProps } from "antd";
import type { ReactNode } from "react";

type IconActionProps = Omit<ButtonProps, "aria-label" | "children" | "icon"> & {
  label: string;
  icon: ReactNode;
};

export default function IconAction({ label, icon, size = "small", ...props }: IconActionProps) {
  return (
    <Tooltip title={label} placement="top">
      <Button
        {...props}
        className={["icon-action", props.className].filter(Boolean).join(" ")}
        type={props.type ?? "text"}
        size={size}
        icon={icon}
        aria-label={label}
      />
    </Tooltip>
  );
}

export type { IconActionProps };
