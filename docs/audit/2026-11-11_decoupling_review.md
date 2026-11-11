# 2026-11-11 代码审查与解耦重构

## 范围与结论

本轮对后端 Python 内部导入关系、模块规模、既有架构文档、Web 聊天页面、知识库和运行时关键链路做了审查，并执行静态检查、构建及离线测试。AST 扫描覆盖修改后的 289 个 Python 文件、1142 条内部导入边（提取知识状态模块之前的快照，包含函数内惰性导入）。双向导入表示维护耦合，不等价于运行时必然发生循环导入错误。

本轮落实三组职责分离；未变更 API、数据库结构、工具执行顺序、审批/遗忘策略、消息缓存 key 或状态语义。没有声称完成所有模块的逐行审计，也未验证真实 PostgreSQL、模型服务、Android/桌面打包和浏览器交互。

## 已完成的修改

### 共享运行时契约

原链路为 `agent_graph → context_assembler → agent_graph.AgentGraph._build_stable_contract`，上下文层通过私有静态方法依赖图编排实现。现在两层均使用 `runtime/system_contract.py`。身份文本和系统契约渲染内容保持一致；旧静态方法和 `_build_runtime_identity` 导入保留兼容入口。

工具流事件匹配和执行回执解析移至 `runtime/tool_events.py`，该模块仅使用标准库。`agent_graph` 保留 `_StreamToolCallMatcher`、`ToolExecutionReceipt` 和 `parse_tool_execution_receipt` 的旧导入路径，避免破坏现有使用方。新实现可独立测试，无需初始化数据库、LangGraph 或模型客户端。

### 知识生命周期定义

原链路为 `ingestor → chunk_vectors → ingestor.INACTIVE_STATUSES`。新增 `knowledge/document_status.py`，统一定义活跃、可恢复、失效和全部状态集合。摄取、向量索引、维护、调度与知识 API 直接依赖该模块，`ingestor` 继续导出旧常量。状态取值与集合类型均保持不变。

### 聊天页面职责拆分

从 `ChatPage.tsx` 提取约 200 行，分为共享消息类型 `types.ts`、状态映射和 ID 生成 `messageModel.ts`、缓存 `messageStorage.ts` 与独立工具卡片 `ToolCallCard.tsx`。

页面仍负责流式请求、历史恢复和 WebSocket 编排；缓存仍保留最近 200 条消息、截断超过 4000 字符的工具结果并兼容旧消息 ID；未知工具状态仍显示待确认。没有调整渲染样式和交互。

## 后续优化建议

| 优先级 | 证据与问题 | 建议和验收条件 |
|---|---|---|
| 高 | 首轮全量测试出现 SQLite `turn_records.cancel_requested_at` 缺失；同组上下文测试单文件运行通过；迁移 `b3e6f9c2d7a4` 已存在 | 调查测试库初始化、PID 文件复用及 schema 隔离；不要据此删除生产模型字段。验收为多次全量与单文件测试一致 |
| 高 | `tools/local_shell.py::resolve_bash` 只验证候选文件存在；本环境选中 `C:\Windows\system32\bash.EXE` 后版本探测为空，原始 HEAD 抽样也有 16 项 Shell 失败 | 对候选执行有超时的 Bash 探测；无效时继续查找 Git Bash。区分环境不可用与执行失败，并增加 Windows 候选回退测试 |
| 中 | `runtime/tool_events.py::parse_tool_execution_receipt` 只在内容以 `{` 开头时解析；目前仅显式处理 queued/running/execution_unknown，其他执行状态默认依赖 ok | 定义严格回执协议，覆盖前导空白、cancelled/preempted/未知状态。先核对所有生产返回值再统一映射，避免把取消或未知状态显示为完成；本轮为兼容保留现状 |
| 中 | `messageStorage.ts::loadStored` 对 JSON 采用类型断言；混合非法条目会触发整批回退空缓存；保存失败被静默忽略 | 增加逐条校验和缓存版本迁移；可提供低打扰的持久化失败提示。验收损坏条目不影响合法历史，并保持服务端历史为准 |
| 中 | `ChatPage.tsx` 拆分后仍集中流式、WebSocket、历史刷新、审批等状态 | 后续按 `useConversationHistory`、`useConversationSocket`、`useTurnStream` 分批提取；先补首个状态快照、重连、finally 延迟刷新和取消竞态测试 |
| 中 | `worker/outbox_handlers.py` 约 2638 行，混合记忆提取、提醒、维护、压缩和 epoch；`handlers_forget` 导入其私有摘要函数 | 将纯摘要构建/解析放共享模块，再按领域拆 handler；保留注册入口。用重试、fencing、取消与幂等测试验证事务行为 |
| 中 | `memory_write_service ↔ conflict_confirmation`、`content_scope ↔ content_scrub`、多组 builtin handler ↔ 注册入口仍双向依赖 | 将策略/类型/公共机制下沉；通过依赖注入调用业务操作。优先处理纯函数和协议，涉及遗忘屏障、SessionLocal monkeypatch 与事务提交时保留兼容契约 |
| 低 | `db/models.py` 约 2249 行，横跨多个业务域；旧编码规范与实际技术栈存在年代差异 | 模型按域拆分时保留统一模型导入注册，保证 metadata 完整和迁移不变；文档另做版本化清理，避免把历史设计当作当前实现 |

这些建议中，回执语义、损坏缓存恢复及 Bash 探测属于功能健壮性改进；其余主要是维护边界改进。本轮没有为完成拆文件而改动高风险事务。

## 验证

- `ruff check backend/aiive --select F,E9`：通过。
- `basedpyright --pythonpath .venv/Scripts/python.exe`：0 errors / 0 warnings。
- `cd frontend && npm run build`：TypeScript 检查与 Vite 构建通过。
- `cd frontend && npm run test:chat`：4 项通过，覆盖未知状态、旧历史恢复、缓存限额、不可用存储。
- 运行时、上下文、工具输出和记忆审计定向回归：91 项通过。
- 知识摄取、维护、向量投影及模块边界定向回归：50 项通过（含重复的边界测试）。
- 新增子进程模块边界测试，防止共享契约重新引入数据库和执行栈依赖；最终边界与 builtin MCP 定向测试 4 项通过。
- 首轮全量：1768 passed / 68 failed / 131 skipped。对应上下文单文件：14 passed。最终重跑为 **1821 passed / 16 failed / 131 skipped**；未修改 HEAD 在独立目录、相同依赖和配置下为 **1819 passed / 17 failed / 131 skipped**。最终 16 项失败均在 HEAD 对照失败集合中，全部为本环境 Bash/Shell 测试；没有新增失败节点。HEAD 额外的 MCP 激活断言失败在最终工作区与独立定向测试中未复现。首轮 52 项非 Shell 失败在最终重跑与 HEAD 对照中均未复现，根因仍待单独定位，不认定为产品缺陷。

全量日志保存在本地 `.pytest_cache/refactor_validation*.log`，原始 HEAD 对照在 `.pytest_cache/refactor_baseline_full.log`。日志未纳入版本管理。前端构建产物只用于验证，未作为源码改动提交。未提交或推送 Git。


## 同日跟进：测试问题修复

用户要求修复测试问题后，完成以下修改：

1. **修复 Bash 解析的实际功能缺陷**：本机 Git 安装在 `E:\git\Git`，PATH 只有 `Git/cmd`；原解析器选中了不可用的系统 Bash 启动器。现在从 PATH 的 Git 推导 `bin/bash.exe` 与 `usr/bin/bash.exe`，并要求候选通过有限时长、环境隔离的 GNU Bash 版本探测。失败继续查找，成功结果缓存。没有跳过或弱化真实 Shell 测试。
2. **消除旧 schema 复用风险**：测试库由仅 PID 命名改为 `mkstemp` 独占创建、PID 加随机后缀。新进程不会复用异常退出留下的同 PID 文件。新增子进程测试验证新库独立、旧文件不变和正常清理；原有成功、失败及清理异常退出测试保留。此修改消除一条明确风险，仍不能据此断言首轮缺字段异常的历史根因已被完全复现。
3. **修复 MCP 测试入口定位**：固定内置入口从实际加载的运行时模块推导，避免测试文件所在 checkout 与 Python 可编辑安装目录不同导致可信路径误报。生产信任校验不变，断言增加失败结果详情。

验证：Shell、输出流式日志和进程数据库清理 **49 passed**；MCP **3 passed**；Ruff 通过；basedpyright **0 errors / 0 warnings**。最终全量测试 **1844 passed / 131 skipped / 0 failed**，耗时 314.41 秒。跳过项保留既有 PostgreSQL/平台条件，未新增跳过来规避失败。详细日志：`.pytest_cache/test_fix_full.log`。
