import { theme, type ThemeConfig } from "antd";

/** Dark, low-chroma palette in the spirit of a coding-agent console. */
export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: "#d97757",
    colorBgBase: "#0e0f11",
    colorBgContainer: "#17181c",
    colorBgElevated: "#1c1e23",
    colorBorder: "#2a2d34",
    colorBorderSecondary: "#23262c",
    colorText: "#e6e6e6",
    colorTextSecondary: "#a8adb7",
    colorTextTertiary: "#7c828d",
    borderRadius: 8,
    fontSize: 14,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: { siderBg: "#111214", headerBg: "#0e0f11", bodyBg: "#0e0f11" },
    Menu: { itemBg: "transparent", darkItemBg: "transparent" },
    Card: { paddingLG: 16 },
  },
};

export const MONO =
  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace';
