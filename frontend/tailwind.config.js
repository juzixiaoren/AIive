/**
 * Tailwind CSS 配置
 * - 扫描 index.html 和 src 目录下的所有 JS/TS/JSX/TSX 文件以生成对应的 CSS 类
 * - 当前使用默认主题，可通过 extend 扩展自定义样式
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {},
  },
  plugins: [],
};
