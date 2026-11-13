# V1 聊天附件消息原生化与按需读取验收

日期：2026-11-13。一个 Part 实现，不新增数据库表、迁移、Embedding、持久附件索引或独立执行器。

## 基线与改动顺序

开发前执行 `git fetch origin main`，本地 HEAD 与 origin/main 均为 `808f638d1b8c271adebe3e58d880022d870339ad`，实际阅读上下文、附件、Forget、工具和任务运行时源码。工作区原有 current_phase 与 P1 报告修改予以保留。

先新增共享读取与进度工具，独立测试读取、权限和 SHA256，再接入上下文与 Worker。107 项定向测试、新增 10 项专项测试通过；首轮全量仅发现一处旧工具集合断言，更新后该文件 27 项全部通过。此后才删除 LoadedAttachments/load_attachments/prompt_parts 原件注入代码及 context 配额分支，保留上传与待发送资源配额。清理后重新运行完整 Python 套件。

## 真实调用链

1. 上传、Object Store、SHA256、bind 和 Event.payload.attachment_ids 沿用现有实现。
2. ThreadState._events_to_dicts 以来源 Event ID 批量查询 Attachment，并经 visible_rows 过滤；保留引用在所属 user message。ThreadState 与 ContextAssembler 的消息转换均渲染引用，Token 计数包含元信息。
3. 当前消息按 current_turn_id 查询同样的元信息。ContextOverflowPacker 处理包含引用的完整 Turn，淘汰后不会产生独立附件块；list_thread_attachments 可重新发现历史文件。
4. 修正 TurnExecution 传给历史 keyset reader 的排他上界：使用当前 turn_sequence，避免上一轮被少读。
5. ContextAssembler 不再读取附件原件、提取 PDF/DOCX 或构造历史图片 Base64；TurnExecution 不再根据历史图片更换整个对话的模型。
6. 输入 Snapshot 和读取工具结果 Snapshot 只保留引用、读取参数等元信息。正文仅按需作为工具观察使用，不在附件元信息快照中复制。

## 工具权限与来源

`tools/attachment_tools.py::register_attachment_tools` 同时服务 Main 专用注册表和 Worker 全局注册表。Main 可见 list_thread_attachments/read_attachment；record_attachment_progress 只注册给 Worker，并在 handler 再校验 task_id。模型不能传 thread_id、Object Store key 或服务器路径。

每次操作从 RunContext 取 Thread；使用 Forget 共享锁、require/visible_rows、references_visible；读取验证 get_verified 的文件存在性和 SHA256，失败拒绝。工具不返回 Object Store 路径，所有资料标明为不可信证据。

引用实际进入上下文后，原始 Event 进入 attachment_context；list/read 实际返回的 Event 也追加到当前 Turn 来源。Worker 同时 persist_sources 到 agent_task，因此 TaskReadRepository 与 Worker 的来源门禁在 Forget 后停止任务，Evidence 和 Artifact 继续继承现有来源关系。自动 Memory 屏蔽条件改为本轮实际调用 read_attachment；仅看见历史引用不会永久关闭 Memory。

## 文档与图片

文档读取复用 parse_document_blocks 和 chunk_blocks，target=350/max=450 tokens。query 使用确定性的关键词计数排序和 chunk_index 稳定排序；最多两块。顺序读取一次一块，提供总数、下一个位置、标题路径、页码和行号。无持久化索引，不写 Knowledge Base。

图片读取在 handler 内创建多模态请求：优先 AIIVE_VISION_MODEL 及其 URL/Key，否则只有 AIIVE_LLM_VISION_ENABLED=true 才调用主模型。Base64 只存在于瞬时模型请求，工具返回文字分析与模型名；无视觉配置明确报错。离线测试替换模型网络边界，验证图片和 query 实际进入视觉请求，以及 Base64 未进入工具结果和 Event；不宣称完成真实外部模型连通性测试。

## 全文处理与重启

Main 选择 document_processing，将附件 ID 放入 delegate_task 目标。服务端记录所需附件 ID，Worker 常驻可见附件工具。按 chunk_index 读取后，服务端保存读取 receipt；处理一块后调用 record_attachment_progress，验证连续推进、实际已读取块和总块数。重复提交不覆盖既有摘要或倒退。

每个附件的 next_chunk_index/total_chunks/rolling_summary/processed_chunk_count 位于 AgentTask.task_state.attachment_processing，以附件 ID 为键支持多文件。更新与现有 Checkpoint 同事务提交。恢复上下文显式保留进度字段，避免大 task_state 的通用截断隐藏游标。完整文档任务的顺序读取校验恢复位置，拒绝重复读取已完成块或跳块。

原有租约恢复 recover_stale_tasks 重新调度 Task；新 Worker 从 next_chunk_index 继续。读取完成但尚未记录进度时崩溃，允许重读该未完成块。ToolOutputGuard 对服务端有界附件页使用独立上限，Evidence 摘要完整保留页面；TaskContextAssembler 只对附件 Evidence 放宽回填，避免仅返回 Worker 无法访问的日志路径。

最终沿用 TaskReport/Artifact。所需文件尚无进度或进度未到末尾时，即使模型声明 succeeded，也强制 partial。预算耗尽沿用现有 partial 门禁。测试包含真实 Runtime 的读取、进度 Action、租约过期恢复和最终报告，并验证提前结束被降为 partial。

## 测试证据

日志保存在本地 `build/attachment-*.log`（构建目录不提交）。

- 新专项测试覆盖 Thread 隔离、无效 ID、SHA256、Forget、源消息定位、Token 转换、目录发现、Packer 淘汰、PDF 页码与相关性、Vision 请求、Checkpoint/租约恢复、幂等与单调进度、完整/partial 报告、Evidence 回填和大状态恢复。
- HTTP/SSE 测试保留上传、重复/跨 Thread 绑定、历史 UI、分享、取消、Forget 与上传配额覆盖，并新增 Main 真正调用 read_attachment 及 Memory 屏蔽测试。T2 使用 get_verified 失败探针，证明普通聊天不读原件。
- 最终全量 Python：1998 passed、146 skipped、6160 warnings，378.55 秒，见 `build/attachment-full-tests-verified.log`。清理后的主链路定向回归 72 passed；专项文件共 12 项。
- Ruff：`ruff check backend/aiive --select F,E9`；basedpyright：均已通过，最终检查日志见 build。
- Web 与 Android：各自 `npm run build` 成功。
- 前端契约：test:chat 4 项、test:work 20 项通过；Desktop Node Host 8 项通过。
- 全仓库逐文件隔离：174 个文件全部通过，0 失败；包含需环境条件而跳过的测试文件，不能将文件级通过理解为 PG 用例已执行。
- `git diff --check` 通过。

## CI 环境边界

本地执行了可用的 pytest、Ruff、basedpyright、前端/Android 构建和前端/Desktop 契约门禁。Docker info 失败：Docker Desktop Linux Engine 命名管道不存在，因而 PostgreSQL 服务、Alembic PostgreSQL 空库验证、PG 并发契约和 Compose smoke 未执行。默认全量套件会明确跳过需 PG/POSIX 的测试。

按用户要求提交并推送，不查看或等待远端 GitHub Actions；push 可能自动触发工作流，不能把本地验证描述为远端 CI 全绿；全仓库单文件隔离门禁已完成：174 passed、0 failed（每文件独立 pytest 进程、并发 2），见 `build/attachment-isolation.log` 和 `build/attachment-isolated/`。现有 CI 配置不变，新测试由现有 pytest job 自动收集。
