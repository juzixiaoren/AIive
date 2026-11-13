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

## 首版 V1 测试证据（c982d67）

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


## V1 权限隔离与读取性能收尾

本次基线为 `c982d67ed3526fac846c37051df3d4d81d951fc6`，一个 Part 完成。保留上传、历史恢复、Forget、GC、Memory、本地路径文档和 Knowledge Base 实现。Task 附件范围改为结构化显式授权，不兼容旧的目标文本隐式授权；缺少授权的旧任务拒绝读取，需显式传 attachment_ids 创建新任务。

### 授权执行链

`delegate_task(attachment_ids=[...], skill_id="document_processing")` → initial_input.attachment_ids → TaskRepository 在共享 Forget 锁内调用 validate_task_attachments → require/visible_rows、真实 user Event 与 Turn 同 Thread 绑定、references_visible → required_attachment_ids 精确名单及 agent_task ContentProvenanceRef → 初始 Checkpoint。

Worker 的 `_session` 先检查 Task Thread、task_visible、Skill 及 initial_input/required 名单一致性。list 在 SQL 中先限定授权名单，read/progress 在任何原件或缓存访问前拒绝未授权 ID。空名单不能访问任何聊天附件；非 document_processing Skill 的 Worker 附件工具调用被拒绝。Main 无 task_id 时保持 Thread 全部可见附件访问能力。全文完成遍历 required_attachment_ids，各文件必须具备完整 total/next/processed 进度；不再分析目标文本字符串。

### Vision 事务与竞态

预检事务中完成权限、原件/SHA256、来源入库和 Forget revision 读取，提交并关闭后才调用 Vision。结束后重新开事务取得共享锁，核对 revision、来源可见性、绑定、SHA 和 Task 可见性；任何不确定或变化均拒绝结果。Base64 只存在于瞬时模型请求。

新增 SQLite 测试在模型 callback 断言 Session.in_transaction=false，并模拟 Forget 和 revision 变化。新增 PG Main/Worker 两用例在 callback 的另一 Session 执行 Forget Phase A（lock_timeout=2s）；旧跨网络持锁实现会阻塞并失败，新实现应获得排他锁并在复检拒绝分析。

### 缓存约束

进程内不可变解析 Chunk LRU：总计费上限 16 MiB、最多 16 项、单项最多 4 MiB。键至少包含 attachment_id、sha256、PARSER_VERSION、CHUNKER_VERSION，另含 object_key、filename、Object Store 根、350/450 切块配置。递归计费所有可达 Python 对象，额外预留每项 512 字节；过大结果不缓存，正常返回当前页。RLock 保护读取、LRU 淘汰及 miss 解析，同一文件并发 miss 只解析一次。原始字节不常驻缓存，不增加表、索引、Embedding 或外部服务。

每次缓存访问前重新执行 Thread、绑定、Task 授权和 Forget 可见性检查。SHA 或 parser/chunker 版本变化使用新键，绝不复用旧结果。被遗忘内容即使命中缓存也不能返回。正常 Chunk 翻页在命中后不再读取、校验和解析整个原件。query strip 后为空使用顺序读取；所有相关性分数为 0 时返回空 chunks 和 matched=false；chunk_index >= total_chunks 报 invalid_chunk_index。

### 新增证据与 CI

- 新专项 `test_attachment_isolation.py` 本地 21 passed；涵盖 A/B 隔离、list/read/progress、无授权、错误 Skill、多授权文件、创建时原子拒绝、来源提前登记及 Forget 停止、Main 不限名单、中文目标的结构化全文判定、零命中、空白 query、EOF、SHA/版本失效、并发单次解析、内存淘汰和 Vision 事务边界。
- 原附件/Task/Forget 定向回归 85 passed，原断点恢复测试已使用显式 attachment_ids。
- Ruff（F,E9）与 basedpyright 均零错误、零警告；Web/Android 构建成功，前端 chat 4、work 20、Desktop Node Host 8 项通过。
- 本地全量 Python 2017 passed / 146 skipped / 6160 warnings，393.81 秒；之后新增的多附件完成门禁与 PG 竞态用例通过 21 项专项回归（本地 PG 14 skipped）。GitHub CI 已完成，详见下表；日志前缀为 `build/attachment-isolation-*`。GitHub CI 按本次提交 SHA 验证，覆盖 pytest、单文件隔离、Ruff、basedpyright、前端/Android、Alembic、integration、postgres-contract 和 docker-smoke 共 9 Job。


### 实现提交的完整 GitHub CI 结果

实现提交：`410d9cfc4b308ac657c54377d1fe6fcd50bd38a8`。运行：[CI #99](https://github.com/juzixiaoren/AIive/actions/runs/43386265325)，最终 conclusion=success，9/9 Job 通过。

| Job | 结果 |
|---|---|
| 单元与关键流程 pytest | 1380 passed / 1 skipped，162.98 秒 |
| 本地集成 pytest | 643 passed / 143 skipped，75.96 秒；PG 专项在独立 Job 执行 |
| 单文件隔离 | 全部 175 个文件通过 |
| PostgreSQL 核心契约 | 104 passed / 39 skipped，53.96 秒；新增 Main/Worker Vision 锁竞态两项均 PASSED |
| Ruff | success |
| basedpyright | success |
| 前端与 Android 构建、跨端契约 | success |
| Alembic 空库升级与 metadata check | success |
| Docker Compose smoke | success |

PG Job 的 39 项跳过来自可选 pg_search 镜像与外部 Embedding 环境条件，不包含新增 Vision 锁竞态用例。以上是实现提交的 CI 证据；后续仅补充验收文档，不修改已验证的实现。
