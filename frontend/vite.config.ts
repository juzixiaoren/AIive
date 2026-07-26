/**
 * Vite 构建配置
 * - 配置 React 插件
 * - 设置开发服务器端口和 API 代理规则
 * - 生产部署在域名子路径 /aiive 下，故 base 默认为 /aiive/
 *   本地开发可设环境变量 AIIVE_BASE_PATH=/ 还原为根路径
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 部署路径前缀：生产挂在 /aiive，本地开发可设 AIIVE_BASE_PATH=/ 还原根路径
const base = process.env.AIIVE_BASE_PATH || "/aiive/";

export default defineConfig({
  base,
  plugins: [react()],
  server: {
    port: 5173,
    // 代理配置：将 /api、/health 和 /ws 请求转发到后端服务
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/ws": {
        target: "http://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
