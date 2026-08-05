import type { ThemeConfig } from "antd";

const systemFontStack =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

export const oceanTheme: ThemeConfig = {
  cssVar: { prefix: "mini-agent" },
  token: {
    colorPrimary: "#087f8d",
    colorInfo: "#087f8d",
    colorSuccess: "#16a34a",
    colorWarning: "#d97706",
    colorError: "#dc2626",
    colorText: "#1f2329",
    colorBgLayout: "#f4f7f8",
    borderRadius: 10,
    fontFamily: systemFontStack,
  },
};

export { systemFontStack };
