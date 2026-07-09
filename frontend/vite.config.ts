/**
 * Vite 构建配置
 * - 配置 React 插件
 * - 设置开发服务器端口和 API 代理规则
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 代理配置：将 /api 和 /health 请求转发到后端服务
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
});
