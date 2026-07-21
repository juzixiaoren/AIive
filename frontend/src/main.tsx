/**
 * 应用入口文件
 * - 使用 React 18 的 createRoot API 挂载根组件
 * - 启用 StrictMode 进行开发时的额外检查
 * - 加载全局 Tailwind CSS 样式
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
// 代码块语法高亮主题（rehype-highlight 依赖 highlight.js 的配色）
import "highlight.js/styles/github.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
