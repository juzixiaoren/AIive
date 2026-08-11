/**
 * Android 应用配置。
 *
 * 首次构建前填写服务器根地址，例如 http://192.168.1.10:8000。
 * 这里故意留空，避免生成的 APK 意外连接到开发者机器。
 * 也可以在构建环境中通过 VITE_AIIVE_API_BASE_URL 临时覆盖。
 */
const DEFAULT_BACKEND_BASE_URL = "";

export const appConfig = {
  backendBaseUrl: (
    import.meta.env.VITE_AIIVE_API_BASE_URL || DEFAULT_BACKEND_BASE_URL
  ).trim().replace(/\/+$/, ""),
  storageKey: "aiive.android.chat.v1",
  themeKey: "aiive.android.theme.v1",
} as const;
