/**
 * 部署路径前缀
 * - 与 vite.config.ts 的 base 保持一致
 * - vite 在构建时注入 import.meta.env.BASE_URL（例如 "/aiive/"）
 * - 用于子路径部署时拼接前端路由、API 与 WebSocket 地址
 */
const env = (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env;
export const BASE_PATH = env?.BASE_URL ?? "/";
