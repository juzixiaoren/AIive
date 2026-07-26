/**
 * 应用入口文件
 * - 使用 React 18 的 createRoot API 挂载根组件
 * - 启用 StrictMode 进行开发时的额外检查
 * - 加载全局 Tailwind CSS 样式
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// 代码高亮 token 配色随主题切换，定义在 index.css（不再引入 highlight.js 的静态浅色主题，
// 否则深色模式下深色背景配浅色主题 token，代码几乎不可读）
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
