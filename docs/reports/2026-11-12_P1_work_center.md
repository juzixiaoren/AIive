# P1 统一工作中心验收报告

日期：2026-11-12。实施基线：main `8038182`，包含原探索基线 `5a7e799` 后的安全读取契约和委派卡片补充。

## 交付范围

Backend、Web、Android 使用同一 Thread、AgentTask 和 WorkReadModel，形成聊天委派、发现工作、查看详情、处理事项、刷新状态及获取成果的完整功能链。Desktop 继续复用 Web。没有新增表、迁移、Task 状态机、Notification 功能、系统推送或 Agent Evaluation。

| 内容 | 实现 |
|---|---|
| 统一读取 | Inbox 四分类及 cursor 分页、WorkView 安全详情、Timeline 原始序列增量 |
| 待处理事项 | Action 审批、Task 输入、Goal 确认、Question 三种意图、步骤启动与步骤授权、受阻目标处理 |
| Goal 操作 | 确认、修改、拒绝、暂停、恢复、结束；编辑说明、截止时间、时区、验收标准、步骤内容/方式/依赖/证据要求 |
| 展示 | 实际 Goal 步骤进度、target_device 关系及在线状态、时间线、报告、成果与证据、折叠 Action 摘要 |
| 聊天连接 | 独立 agent_task_created 卡片及历史恢复；普通 task_event 不再生成聊天消息，ProactiveMessage 保留 |
| Android | 独立移动界面、底部对话/工作、ChatApp 持续挂载与 SSE 保留、Thread 订阅互斥 |
| 文件 | Artifact/Evidence 每段最多 1 MiB、实际 offset、整文件增量 SHA256、文件名和 MIME 限制、原生 Documents 保存/系统分享 |
| 同步 | Thread work_changed 失效、重复版本去重、串行刷新、页面选择竞争保护、连接/网络/前台恢复快照 |

## 最终 API 契约

读取模型继续使用 `TaskReadRepository` 的 Task、计划及子资源 Forget 校验。WorkView 不输出原始 arguments、TaskState、执行上下文、object_key、object_bucket 或 inline_payload。

- `GET /api/agent-tasks/inbox?bucket={attention|active|waiting|ended}&limit=&cursor=`：返回 items、next_cursor、has_more，游标不可跨分类或 Thread 使用。
- `GET /api/agent-tasks/{id}/work-view`：task、bucket、attention_items、goal、current_activity、target_device、timeline、artifacts、evidence、actions、truncated、last_sequence。Goal progress 直接使用 GoalPlan.progress；普通 Task 不生成百分比。
- Action 摘要复用 project_many 的现有可见记录，只返回 id、sequence、capability_id、status、risk_level、requires_approval、created_at、started_at、completed_at。查询仍按每状态最多 32 条有界；不承诺全量审计列表。
- `GET /api/agent-tasks/{id}/timeline?after_sequence=&limit=`：返回 items、next_sequence、has_more；高水位扫描包括内部或不可见事件形成的序列空洞。Timeline 与 WorkView 共用静态事件投影，摘要不复制任意 payload。内部问题账本仍不可见。
- Attention allowed_actions 仅用于 UI 提示，服务端写接口继续独立校验。goal_question 支持 answer、modify_plan、reject；reject 跳过当前步骤。Action 拒绝与 Goal Step 授权通过不同入口。
- 原有 Approval、Input、Cancel、Goal 控制、Question answer、Step start/approve、Evidence 和 Artifact 写读接口保留。已有步骤的部分编辑按 ID 合并未提交字段，保留安全视图省略的触发条件；内容变更及下游失效继续由 GoalService 处理。
- 下载暴露 Content-Disposition、X-AIive-Content-Hash、X-AIive-Truncated、X-AIive-Next-Offset。客户端同时验证元数据哈希和实际整文件哈希。

## 同步与缓存

工作页通过 `/ws/{thread_id}` 首帧认证。work_changed 只表示失效，客户端不从中恢复业务状态；按 task_version/event_sequence 去重，合并刷新 Inbox 和选中 WorkView，再从快照原始高水位以 Timeline after_sequence 读取增量并按 sequence 去重。

刷新串行运行，选择版本阻止旧响应覆盖新任务；分页响应也校验快照版本。权威刷新重新建立列表分页及早期时间线缓存，防止已经被 Forget 隐藏的内容留在旧缓存中。用户可继续按需读取早期时间线。连接断开使用有上限退避重连；重连、网络恢复及恢复前台均重新读取 REST。没有固定业务轮询。

Web 和 Android 的 ChatApp 保持挂载；工作页激活时暂停聊天 Thread 订阅，回到聊天重新订阅并同步历史。本地 SSE 仍由聊天组件持有，不与 Thread socket 的开关绑定。成功委派卡片在 SSE 完成和历史恢复中复用映射，按 task_id 去重；提醒 task_created 不参与映射。

## 技术选型及取舍

| 选择 | 收益 | 代价或边界 |
|---|---|---|
| 沿用 FastAPI / SQLAlchemy / WorkReadModel | 保留已有授权、Forget、事务合并及执行边界 | 读取模型有界，详情展示可见的近期记录 |
| 独立 React UI，共享纯 TypeScript 逻辑 | 移动布局独立，API、操作语义和下载算法一致，可在 Node 执行生产逻辑测试 | 两端各维护展示组件和轻量 hook 适配 |
| React useSyncExternalStore + 小型控制器 | 不增加状态库；集中处理刷新竞争、游标与去重 | 权威刷新会重置已加载的早期分页，可按需重新读取 |
| Thread WS 失效 + REST 权威快照 | 复用既有通道，客户端不维护第二套业务状态 | 断线期间不保证即时展示，恢复后收敛 |
| 官方 Capacitor Filesystem / Share / App 8.x | 直接支持系统文件、分享和前后台生命周期；不引入自建原生桥 | 原生部分仍依赖 OS 权限及文件系统行为 |
| @noble/hashes 增量 SHA256 | 原生逐段写盘时无需整文件驻留内存 | 两个客户端各增加同一小型依赖 |
| Web 有界 Blob，Android 原生逐段缓存 | Web 下载兼容现有部署，原生大文件内存有界 | Web 下载上限 128 MiB；预览上限 8 MiB；其他文件类型仅保存/分享 |

文件名做 Unicode 规范化、路径/控制字符替换、系统保留名处理及 UTF-8 长度限制，放在随机目录内。预览仅允许纯文本、Markdown 文本、CSV、JSON、PNG、JPEG、GIF、WebP；不执行 HTML/SVG。下载、校验或保存失败清理本次文件；成功分享的 Cache 文件交由系统清理，避免接收应用读取 URI 前删除文件。

官方接口依据：[Filesystem v8](https://capacitorjs.com/docs/apis/filesystem)、[Share v8](https://capacitorjs.com/docs/apis/share)、[App v8](https://capacitorjs.com/docs/apis/app)。

## 验证证据

| 检查 | 真实结果 |
|---|---|
| 完整 Python：`pytest tests -q` | 1943 passed、134 skipped、5280 既有 warnings，440.23 秒 |
| 真实 PostgreSQL：`AIIVE_PG_TESTS=1 pytest tests/integration/pg -q` | 116 passed、13 skipped，301.66 秒；包含 SAVEPOINT/事务广播 |
| 新增 P1 后端契约文件 | 19 passed，9.13 秒；覆盖真实 Goal 事件、Question allowed_actions、脱敏编辑与 Forget、Timeline 安全、持久历史卡片、CORS |
| 聊天 Node 逻辑 | 4 passed |
| Desktop Node Host | 8 passed（locks + journal） |
| 跨端工作 Node 逻辑 | 14 passed；执行生产 API/控制器/下载/原生适配器/SSE 解析器，替换网络与原生边界 |
| Ruff | backend F,E9 通过 |
| basedpyright | 0 errors / 0 warnings / 0 notes |
| Web / Android | TypeScript 与 Vite 最终生产构建均通过 |
| Capacitor | cap sync android 通过，App/Filesystem/Share 三个原生插件引用已生成 |
| CI | 保留 9 个 Job；原前端 Job 加入 Android npm ci、TypeScript/Vite 和聊天/工作 Node 测试；推送后记录运行结果 |

Python 默认跳过 PG 专属、外部 Embedding 及 Windows 不适用的 POSIX 用例；单独 PG 运行的 13 项跳过均为外部 Embedding 不可用。通过结果不包含真实外部模型调用。

按用户要求，最终验证不使用浏览器测试，也不增加人工三端联调项目；未将原生插件边界模拟宣称为 Android 真机验证。未生成新的 APK。旧 API Client 和 Task WebSocket Hook 在确认无其他调用方且新逻辑验证通过后清理；实际 Diff 检查排除生成的 dist 文件与测试临时文件。


## P1/P2 收尾修复（2026-11-12）

经当前 main 代码核实后修复以下实际缺陷，全程未使用浏览器或浏览器自动化：

- 工作中心公共 Thread 连接首帧、running/busy → idle、重连/网络/前台恢复均触发权威刷新。
  重复 idle 不刷新，running/busy 切换不误判结束；work_changed 可见性过滤保持不变。
- Inbox、WorkView、Timeline 的请求失败独立处理；成功数据保留，失败分区清除未经验证旧缓存。
  generation / snapshot / 选择竞争 / sequence 去重仍有效。
- Prompt、来源 Event IDs、附件安全快照共用一次附件加载结果；pending 上传不再污染其他 Turn。
- Chat Attachment 纳入中央 Scrub / Verify 字段覆盖与对象引用计数；清除文件名、MIME、大小、hash 和对象键。
  Object Store 统一通过持久 ForgetAction(delete_object) 在 COMMIT 后处理，失败保留重试，存在共享引用不删除。
- 启动及每小时维护执行幂等历史补偿：仅删除 attachment_context 对 attachment_uploaded 的错误来源引用；
  对既有冻结 Forget selector 补做附件 Scrub，记录修复 Action 并提交后处理对象。合法用户消息来源不删除。
- started 仅表示 Turn 已创建。已知 Agent 执行前失败通过 draft_recoverable 标记恢复草稿，保留本地 File，
  作废失败 Turn 的旧附件 ID，由用户主动重传获得新 ID；未知执行结果不自动重试。
- Thread 累计默认 100 个/100 MiB；pending 默认 10 个/40 MiB，均可用 AIIVE_ATTACHMENT_THREAD_MAX_*
  与 AIIVE_ATTACHMENT_PENDING_MAX_* 配置。对象读取、Base64 构造之前预检查；Token 预算仍走原全局 Packer。
- 历史分页只查询本页 Event 的附件，并批量校验可见性；新增 event_id 索引，Alembic head d5e8f102b304。
- 上传失败、移除和 GC 清理上传 Event 的无引用敏感元数据，保留审计 ID；对象删除遵循提交后执行与共享引用检查。

验证：完整离线回归 1977 passed / 141 skipped / 6162 warnings（391.48 秒）；后续补充历史原件补偿用例已进入
聚焦测试与 CI（最终附件/Forget 聚焦 35 passed）。Node 聊天 4 项、跨端工作/草稿/传输 19 项；Web/Android TypeScript+Vite 生产构建通过。
新增真实 PostgreSQL 附件事务/回滚、选择器、共享对象与历史补偿契约，随既有 PostgreSQL Job 运行。
本机 Docker daemon 未运行，PostgreSQL 证据以最终 GitHub CI 为准；保留现有 9 Job 与测试门禁。

历史生产数据的补偿在运行新版后端维护任务时执行；本次没有直接修改生产数据库或手工删除原件。
保存/分享原生插件仍使用既有官方插件边界测试，本次不宣称 Android 真机验证，也不新增外部模型或 Agent Evaluation。
