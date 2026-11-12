# Android 应用构建指南

## 部署边界

Android 应用是独立分发物，不部署到服务器。生产环境只部署：

- `backend/`：FastAPI 与 Agent 运行时
- `frontend/`：完整 Web 管理与对话页面

APK 内置独立的对话和工作 UI，通过 HTTP(S) 调用现有服务端接口。
工作中心使用 WorkReadModel，与 Web 共用 Task 和 Thread；共享纯 TypeScript
业务模块，不加载 Web 页面。移动端不包含记忆、能力、事件和开发者页面。

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
- 四分类工作中心、工作卡片直达详情、Goal 真实进度与计划编辑
- Action 审批、Task 输入与取消、Goal 生命周期及问题回答/修改/跳过
- 设备状态、时间线、报告、成果保存与系统分享
- 浅色 / 深色主题
- Android 安全区域与原生触控尺寸

有意不包含：

- `trace_id` 与 `thread_id` 元信息展示
- 聊天中的底层执行事件和普通工具结果（技术详情在工作中心查看）
- 记忆、能力、事件、上下文、检索、通知和开发者页面
- 第三方推送与后台常驻连接

## 扩展新的应用形态

`scripts/build-app.sh` 按 target 分发到 `scripts/app-builders/<target>.sh`。Electron
桌面端已经以 `desktop.sh` 接入；未来新增其他客户端时保持同一构建器契约即可。


## 工作同步与聊天生命周期

`MobileShell` 始终挂载 `ChatApp`，底部导航只隐藏对话，保留进行中的 SSE。
对话页激活时订阅 Thread WebSocket，工作页激活时由工作中心接管同一个
`/ws/{thread_id}`；回到对话后重新订阅并同步历史。真正的 `new_message` 仍进入聊天。

`work_changed` 只通知数据失效，客户端按 task_version/event_sequence 合并重复信号，
刷新 Inbox 和 WorkView，再从 WorkView 的高水位用 Timeline `after_sequence` 补齐。
序列去重、串行刷新和选择版本检查阻止并发响应覆盖新页面。重连、网络恢复、恢复前台
及官方 App 插件的 `appStateChange` 触发权威快照。刷新会重新建立分页与早期时间线
缓存，以移除 Forget 后已经不可见的内容；没有固定间隔业务轮询。

## 文件保存与分享

使用官方 `@capacitor/filesystem`、`@capacitor/share`、`@capacitor/app` 8.x；新增
插件后运行 `npm run sync`，原生 Gradle 引用已入库。

1. Artifact 和 Evidence 每次以 offset 和 `max_bytes=1048576` 请求，按实际字节推进。
2. 检查 Content-Hash、Next-Offset、Truncated，增量计算完整 SHA256。
3. 逐段写入 `Directory.Cache/aiive-work/{随机ID}/{安全文件名}`，不将整文件放入 JS 内存。
4. 整文件 hash 与 WorkView 一致后，才能获取保存、预览和分享操作对象。
5. 保存复制到 `Directory.Documents/AIive/{随机ID}/`；分享用 Cache URI 调用
   `Share.share()`。Android 10 及以下按需请求权限，较新系统由官方插件适配。
6. 网络、分段、校验或保存失败清理本次文件。成功分享的缓存由系统管理，避免接收应用
   读取 URI 前文件被删除。

文件名做 Unicode 规范化，替换路径分隔符、控制符、危险字符与系统保留名，限制 UTF-8
长度，保存在随机目录内。预览最多 8 MiB，白名单为 text/plain、text/markdown、text/csv、
application/json、image/png、image/jpeg、image/gif、image/webp。文本按纯文本展示；
其他类型支持保存和分享。Web/开发预览环境使用校验后的 Blob，限制 128 MiB；原生
下载逐段写盘。

## 自动验证

```bash
cd apps/android && npm ci && npm run build
cd ../../frontend && npm run test:work
```

Node 测试执行生产请求构造、同步控制器、下载算法与原生适配器，仅替换传输、
Filesystem、Share 边界。自动验收不依赖浏览器，也不增加人工三端联调项目。
