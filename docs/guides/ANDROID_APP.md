# Android 应用构建指南

## 部署边界

Android 应用是独立分发物，不部署到服务器。生产环境只部署：

- `backend/`：FastAPI 与 Agent 运行时
- `frontend/`：完整 Web 管理与对话页面

APK 内置独立的移动端对话 UI，通过 HTTP(S) 调用服务器上的
`/api/chat/stream`、`/api/thread/reset` 与 `/api/approval/respond`。移动端不会加载
或复用 Web 页面，也不会打包记忆、能力、事件、普通工具卡片或开发者页面；仅保留
远程桌面删除、Shell 等高危操作所需的审批卡片。

## 配置服务器地址

编辑 `apps/android/src/config.ts`：

```ts
const DEFAULT_BACKEND_BASE_URL = "https://aiive.example.com";
```

填写服务根路径，不要在结尾添加 `/api`。也可以在 CI 中临时覆盖而不修改源码：

```bash
VITE_AIIVE_API_BASE_URL=https://aiive.example.com \
  ./scripts/build-app.sh android release
```

默认地址为空，空配置 APK 可以正常启动并展示配置提示，但发送按钮保持禁用，
不会意外连接到开发者机器。

后端默认允许 Capacitor WebView 的 `https://localhost` 来源。若生产部署还需要
其他 Web 来源，通过 `.env` 配置逗号分隔的来源列表：

```dotenv
AIIVE_CORS_ORIGINS=https://aiive.example.com,https://localhost,http://localhost,capacitor://localhost
```

Android 工程目前允许明文 HTTP，便于局域网 IP 调试。生产配置应优先使用 HTTPS；
若生产环境始终为 HTTPS，可以在 `AndroidManifest.xml` 中将
`android:usesCleartextTraffic` 改为 `false`。

## 构建

前置条件：

- Node.js 22+
- Python 3.12+（项目依赖会安装品牌资源处理所需的 Pillow）
- JDK 21（macOS 会自动优先使用 Android Studio 自带 JBR）
- Android SDK 36

统一入口：

```bash
./scripts/build-app.sh list
./scripts/build-app.sh android debug
./scripts/build-app.sh android release
```

构建器会依次：

1. 从定稿 Logo 生成 Web、启动页与 Android 各密度图标；
2. 编译独立移动端 React 页面；
3. 执行 Capacitor 同步；
4. 调用 Gradle 生成 APK；
5. 复制产物到 `artifacts/apps/android/`。

debug APK 使用 Android debug key 签名，可直接安装测试。release 输出为未签名 APK，
正式分发前应在安全的 CI/发布环境中使用项目自己的 keystore 签名。

## 客户端范围

保留：

- SSE 流式对话
- 对话本地恢复
- 新建对话与停止生成
- Markdown 消息展示
- 高危工具审批与结果展示
- 浅色 / 深色主题
- Android 安全区域与原生触控尺寸

有意不包含：

- `trace_id` 与 `thread_id` 元信息展示
- 普通工具调用、普通工具结果和非审批操作卡片
- 记忆、能力、事件、上下文、检索、通知和开发者页面
- WebSocket 通知

## 扩展新的应用形态

`scripts/build-app.sh` 按 target 分发到 `scripts/app-builders/<target>.sh`。Electron
桌面端已经以 `desktop.sh` 接入；未来新增其他客户端时保持同一构建器契约即可。
