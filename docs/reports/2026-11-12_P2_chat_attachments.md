# P2 跨端聊天附件实现报告

日期：2026-11-12。Backend、Web、Android 在一个 Part 内实现，Desktop 复用 Web。

## 实现与取舍

- 复用 Object Store 的 SHA256 内容寻址和 safe_delete，不建立第二套文件服务。
- 新增 chat_attachments 元数据表和 Alembic c4d7e9f1a203；原件不进入普通事件或快照。
- 认证 API：POST /api/attachments（raw-body + URL 编码 X-Filename），GET /{id}、GET /{id}/content、DELETE /{id}。
- 服务端绑定当前唯一 Thread；附件在原用户消息 Turn 事务内关联，ID 集合参与 Turn 指纹。
- 下载、绑定和上下文入口均检查 Thread 和中央 Forget 可见性；附件 ID 不暴露对象路径。
- 文档复用 document_reader，拒绝截断提取、无文本层 PDF、伪 MIME/格式、非法文件名、DOCX 解压超限与宏。
- 图片通过 OpenAI-compatible 多模态 content block 直接发送给主视觉模型或独立视觉模型；未配置则明确拒绝。
- 附件完整文本/图片进入 ContextAssembler 的现有 ContextOverflowPacker，无独立 Token 池。
- 为保证连续引用，当前实现每轮重载所有可见聊天附件。总量超窗明确失败；代价是持续 Token 和 IO 成本。
- 后续读取附件的轮次持久记录 attachment_context 来源关系；Forget 阻断原件及派生轮次读取。
- 附件派生回复不作为自动记忆抽取输入，原用户文本仍可按原规则提取；不自动导入知识库。
- 24h 未发送上传与取消/失败关联由上传入口及每小时 GC 回收，同 hash 活跃引用保护共享原件。
- Web/Android 共享选择、图片预览、上传进度、失败重试、多附件、下载校验和历史卡片组件。
- Android 复用官方 Filesystem/Share；Desktop 使用现有 Web，不另建附件系统。
- 客户端保留文本直至 started；同一草稿重试复用 turn_id，历史卡片以稳定附件 ID 展示。

## 验证

- 原聊天与 ContextAssembler 41 项通过。
- 完整离线首轮：1960 passed / 134 skipped / 2 failed（482.92s）；两个失败已修复并通过聚焦回归：
  历史分页 Forget 过滤；认证静态检查将组件 prop 名误识别为认证 URL/头构造。
- 最终附件专项22项 + 中断回归4项：26 passed；附件与认证聚焦此前32项通过。
- 附件专项覆盖各文档/图片格式、安全拒绝、认证、归属、绑定拒绝、完整性、上下文预算、取消、
  TTL/共享对象、历史、重放和 Forget；测试随原 pytest / single-file Job 自动运行。
- 真实模型烟测（独立临时 SQLite/Object Store）：通过真实 SSE 上传 TXT，模型返回文件随机标记及
  17+25=42；后续真实 SSE 追问再次返回同一标记；原件下载成功。未修改生产会话。
- Web 与 Android TypeScript/Vite 生产构建通过；聊天模型 4 项、跨端工作/附件契约 15 项通过。
- ruff F/E9 与 basedpyright 全 Backend 通过；现有 CI 9 Job 全部保留，最终状态以提交 Checks 为准。

## 验收边界

- 当前未配置视觉服务：多模态输入和明确降级由离线模型边界测试验证，未宣称完成真实视觉模型识别验收。
- 本机未发现 Android SDK/JDK21 完整工具链，完成的是移动前端生产构建；未进行 APK/真机系统选择器、保存分享验收。
- 格式校验不执行脚本/宏，不等同于通用杀毒引擎；无文本层 PDF 不内置 OCR。
- 上传上限可下调（解析器当前最大20MiB），附件数可下调（客户端最高5个）。
- 未发送上传以非消息 attachment_uploaded Event 保留 Forget 来源；关联后使用原用户消息 Event。
- 未删除旧实现；保留任务附件与知识库原链路，聊天附件与其职责不同。
- 初始工作区既有 P1 报告与阶段记录改动保持原样，不纳入本 Part 提交。


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


## P1/P2 最终两项验收

批量 Shield 检查现在以附件自身 Thread/上传时间匹配 thread、time_range、all_user_data 和 cutoff；
查询异常关闭读取。原 Event/Turn/Tombstone 校验保留，原始附件读取排除 memory_only 屏蔽。
GC 内部 Operation 通过真实 VerifyReport 与删除/共享引用复查后才填写 verified_at 并收口；
无对象 Action 和旧的 purged-but-unverified Operation 同样可以验证收口，失败保留重试。
普通用户 Forget 异步机制不变，无新表或产品功能。

附件/GC 聚焦 42 passed；中央字段覆盖测试保留；Node 工作/传输 20 项、聊天 4 项通过，
Web/Android 生产构建通过，Ruff 和 basedpyright 通过。新增 PostgreSQL GC/Retention 契约。
未使用浏览器测试。完成最终提交的 9 项 CI 后关闭 P1/P2，不继续扩展阶段范围。
