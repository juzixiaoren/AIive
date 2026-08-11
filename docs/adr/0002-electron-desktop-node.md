# ADR 0002: Electron Desktop 本地执行节点

## 状态

Accepted

## 上下文

AIive 的 Web 和 Android 客户端无法直接使用个人电脑的本地文件系统和 Shell。
个人 Agent 需要在桌面在线时临时获得这些能力，并允许其他客户端远程发起操作。

## 决策

- 增加 Electron Desktop 分发物；渲染器继续使用现有 Web UI。
- 本地权限只存在于 Electron `utilityProcess` 启动的独立 Node Host，永不通过
  preload 暴露给远程页面。
- Node Host 主动连接 FastAPI WebSocket，声明 `desktop_*` 工具、维持在线租约并
  接收调用；个人电脑无需开放入站端口。
- 后端按单个 Turn 创建 ToolRegistry 快照。只有绑定或唯一可解析的节点同时处于
  在线租约和 WebSocket 已连接状态时，才向模型注入桌面工具。
- 普通写入按个人 Agent 的高权限默认直接执行；删除、Shell 和高危能力使用服务端
  冻结参数的持久化审批。Web、Android、Electron UI 均可响应审批。
- 安全删除允许普通路径，默认移入 AIive 私有回收站；磁盘根、用户主目录、主目录
  的父级以及操作系统关键目录不可删除。

## 后果

- 后端必须运行新增 Alembic 迁移，并保持可被 Desktop、Web、Android 访问。
- 多台在线节点时需要显式绑定线程；仅一台在线节点时自动选择。
- 运行安装包的系统用户决定最终 OS 权限。AIive 不自动提权。
- Electron 分发包应在目标操作系统上构建和签名。
