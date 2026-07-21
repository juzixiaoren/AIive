/**
 * Tailwind CSS 配置
 * - 扫描 index.html 和 src 目录下的所有 JS/TS/JSX/TSX 文件以生成对应的 CSS 类
 * - 颜色统一使用“语义令牌 + CSS 变量”方案，便于 light / dark 主题切换：
 *   light 变量定义在 index.css 的 :root / [data-theme="light"]，
 *   dark 变量定义在 [data-theme="dark"]（当前暂复用亮色，待补充）。
 * - 主题色（primary）、危险（danger）、成功（success）、警告（warning）、强调（accent）
 *   均采用嵌套令牌，例如 bg-primary / bg-primary-hover / bg-primary-soft / border-primary-border。
 * @type {import('tailwindcss').Config}
 */
import typography from "@tailwindcss/typography";

export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 中性色
        background: "var(--color-bg)",
        surface: "var(--color-surface)",
        "surface-muted": "var(--color-surface-muted)",
        content: "var(--color-content)",
        title: "var(--color-title)",
        muted: "var(--color-muted)",
        faint: "var(--color-faint)",
        subtle: "var(--color-subtle)",
        divider: "var(--color-divider)",
        code: "var(--color-code)",
        // 主题色
        primary: {
          DEFAULT: "var(--color-primary)",
          hover: "var(--color-primary-hover)",
          soft: "var(--color-primary-soft)",
          border: "var(--color-primary-border)",
          ring: "var(--color-primary-ring)",
        },
        // 危险 / 停止 / 错误
        danger: {
          DEFAULT: "var(--color-danger)",
          hover: "var(--color-danger-hover)",
          soft: "var(--color-danger-soft)",
          border: "var(--color-danger-border)",
          text: "var(--color-danger-text)",
        },
        // 成功 / 完成
        success: {
          DEFAULT: "var(--color-success)",
          soft: "var(--color-success-soft)",
          border: "var(--color-success-border)",
          text: "var(--color-success-text)",
        },
        // 提醒 / 警告（orange 并入）
        warning: {
          DEFAULT: "var(--color-warning)",
          soft: "var(--color-warning-soft)",
          border: "var(--color-warning-border)",
          text: "var(--color-warning-text)",
        },
        // 强调（记忆类卡片等）
        accent: {
          DEFAULT: "var(--color-accent)",
          soft: "var(--color-accent-soft)",
          border: "var(--color-accent-border)",
          text: "var(--color-accent-text)",
        },
        // 主题色之上的反白文字（按钮白字等）
        on: {
          primary: "var(--color-on-primary)",
        },
      },
      keyframes: {
        // 工具卡片入场：轻微回弹的弹簧曲线
        "tool-in": {
          "0%": { opacity: "0", transform: "translateY(6px) scale(0.96)" },
          "60%": { opacity: "1", transform: "translateY(-1px) scale(1.01)" },
          "100%": { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        // 内容淡入
        "fade-in": {
          "0%": { opacity: "0", transform: "translateY(-2px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        // “正在分析”三点跳动
        "thinking": {
          "0%, 80%, 100%": { opacity: "0.2", transform: "translateY(0)" },
          "40%": { opacity: "1", transform: "translateY(-2px)" },
        },
      },
      animation: {
        "tool-in": "tool-in 0.32s cubic-bezier(0.34, 1.56, 0.64, 1) both",
        "fade-in": "fade-in 0.22s ease-out both",
        "thinking": "thinking 1.2s ease-in-out infinite",
      },
    },
  },
  plugins: [typography],
};
