# Current Phase

## Phase
project_plan_v34 - Eval Harness 与 Kernel Contract Checks（先刹车，再加能力）

## Status
completed（离线自进化安全门已实现并落地到 promote 晋升流程）

## Implemented
- `backend/aiive/eval/` 新包：离线 Replay Eval + 内核契约检查 + 晋升安全门。
  - `models.py`：纯数据契约（ContractResult / StepResult / EvalCaseResult / EvalRunResult / GateResult），可 JSON 序列化。
  - `contracts.py`：5 项内核契约（memory 唯一真相源 / key events 持久化 / outbox 幂等 / safe_delete 守卫 / context snapshot 持久化），`run_contract_checks` 单检查异常 fail-closed。
  - `scenarios.py`：24 个离线确定性场景（eval.s01–eval.s24），工具调用 + 失败处理 + 记忆/Event/Outbox/Snapshot 写入断言 + 组合流。
  - `replay.py`：`isolated_eval_session()` 一次性 SQLite + 重定向 `builtin_tools.SessionLocal`；`ReplayRunner` 经真实执行链派发 step；`run_replay_isolated()` 崩溃 fail-closed。
  - `gate.py`：`SafetyGate.evaluate(...)`，契约 + 重放 +（可选）候选子进程，三者全过才 `ok=True`；失败/异常/不完整一律阻断。
  - `persistence.py` + `cli.py`：结果落盘 `runtime/eval_runs/*.json`，`python -m aiive.eval run`。
- `selfdev/promote_rollback.py::promote`：晋升前接入 `SafetyGate`，fail-closed 阻断。
- `api/routes_eval.py`：只读查询 `GET /api/eval/runs`、`/runs/latest`（已挂入 `main.py`）。
- 测试 4 个新文件 19 用例：契约正负例、24 场景全过、闸门阻断/放行、promote 集成、持久化往返。

## Not Implemented（V35+ 候选，按优先级）
- P1 model_router / cost & latency 记录 + 上下文装配统一评分
- `P2 supervisor 真正托管进程`（V37 已完成：spawn/stop/restart + 端口切换 + 事件流）
- 其他：PG 并发契约测试、MaintenanceReport 页面、npm install --ignore-scripts

## Observable Result
- 全量 876 passed / 0 failed（857 基线 + 19 新增）；19 个 eval 用例全过；24 场景重放 passed=24 failed=0。
- ruff：eval/、promote_rollback.py、routes_eval.py、main.py、test_eval_* 全部 All checks passed。
- basedpyright：上述新增/改动文件 0 errors / 0 warnings（与既有 backend 基线一致）。
- 晋升安全门已证明：契约违规 → 阻断；候选子进程失败 → 阻断；干净系统 → 放行并切换活动槽位。

## Tests Run
- AIIVE_MEMORY_VECTOR_ENABLED=false python -m pytest -q --tb=line -ra  → 876 passed, 14 warnings, 107.10s
- python -m pytest tests/unit/backend/test_eval_*.py -q → 19 passed
- .venv/Scripts/python.exe -m ruff check backend/aiive/eval backend/aiive/selfdev/promote_rollback.py backend/aiive/api/routes_eval.py backend/aiive/main.py tests/unit/backend/test_eval_*.py → All checks passed
- /e/python/Scripts/basedpyright backend/aiive/eval backend/aiive/selfdev/promote_rollback.py backend/aiive/api/routes_eval.py backend/aiive/main.py → 0 errors, 0 warnings

## Known Gaps / 残留风险
- 候选子进程评测只覆盖候选的 `aiive/eval` 包重放；未做候选全量 diff 级回归（候选评测本身复用真实执行链，已足够作为“候选能跑通自身评测”的闸门）。
- 契约检查默认跑在隔离库（`isolated_eval_session`）；若传入 `live_session`，则针对现场库检查（promote 默认不传，仅用隔离库验证契约成立性）。
- PG 专属并发语义（advisory lock / SKIP LOCKED / FOR UPDATE）在 SQLite 评测库下未被覆盖，生产 PG 路径以既有集成测试为准。
- 评测库为 SQLite，与生产的 Postgres+pgvector 在少数 SQL 方言上有差异；评测刻意只覆盖不依赖向量检索的纯执行链路径。

## Next Phase
project_plan_v35 - Model Router 与上下文装配统一评分（P1）；或 supervisor 真实托管进程（P2）。

---

## V34.1 PostgreSQL 核心契约测试（完成）

### Implemented
- 新增 `tests/integration/pg/`：8 个真实 PostgreSQL 契约用例 + 隔离夹具（Docker 一次性
  `pgvector/pgvector:pg16` 或 `AIIVE_PG_TEST_URL`，每用例全表 TRUNCATE）。
- 覆盖：并发同键写入只留 1 条 active（advisory lock 真实持有）、并发 claim 不重复（SKIP LOCKED）、
  SKIP LOCKED 跳过锁定行不阻塞、forget cascade 异常回滚无半状态、forget deadletter 三表原子一致、
  写入回滚无记忆/投影残留。
- `pg` marker 注册；默认未启用时收集阶段 skip（0.09s，不启动容器）。
- CI 新增独立 Job `postgres-contract`（pgvector service + `AIIVE_PG_TESTS=1`）。

### Observable Result
- PG 专项：8 passed（连续 3 次稳定，16–18s/次）。
- 默认全量：884 passed, 8 skipped, 0 failed（116.66s）。
- 未发现产品 Bug；修正两处测试自身构建问题（非法 memory_type、FK 插入顺序）。
- 详见 `docs/audit/pg_contract_tests_report.md`。

### Alembic 一致性补验（同日补跑）
- 空库 `alembic upgrade head`：16 个迁移全部成功，head `e2f6a8c4d1b7`。
- 结构对比（迁移后库 vs ORM metadata）：71/71 表，列/类型/可空性/唯一约束/索引全部 0 差异。
- 发现并修复 1 处漂移：HNSW 索引 `ix_memory_vector_embedding_hnsw` 只存在于迁移的裸 DDL，
  已在 `MemoryVectorProjection.__table_args__` 同步声明（`.ddl_if(dialect="postgresql")` 抑制 SQLite），
  `alembic check` 由 FAILED 转为 `No new upgrade operations detected.`，`create_all` 与迁移索引定义一致。
- CI `alembic` Job 增加 `alembic check` 步骤，防止同类漂移再次进入主干。

## V35 统一 Agent 工具发现与分层注入（完成）

### Implemented
- `tools/registry.py`：职责分层——`register/unregister`、`catalog()`（轻量目录，无 Schema）、
  `search()`（本地关键词检索，确定性打分）、`definition()`（单工具完整定义）、
  `list_all()`（仅管理/诊断，docstring 禁止整段注入模型上下文）；执行链未改动。
- `task_runtime/tool_injection.py`：L0 常驻清单（记忆读取/基础搜索/desktop_exec/任务管理）、
  注入模式阈值（≤12 工具且 ≤24k 字符全量注入，否则按需发现）、发现状态（只存 id + 指纹）、
  `build_tool_view`（执行视图 vs 发现视图，`in_scope` 标注，陈旧指纹标记）。
- `control/scope.py` + `task_runtime/router.py`：新增 `capability_policy`（unified/restricted）；
  task_type 不再生成默认能力白名单，Skill Profile 不再缩小发现范围；路径/域名/网络/密钥/节点约束保留。
- `control/capability_broker.py`：`registry_for` 支持 unified；新增 `available_registry`、
  `search_capabilities`、`describe_capability`、`resident_capabilities`（发现不越权）。
- `task_runtime/schemas.py`：`ActionProposal` 新增只读决策 `search_capabilities` / `describe_capability`。
- `task_runtime/runtime.py`：发现决策不创建 AgentAction、不调用 handler，仅记录状态与事件；
  常驻能力注入替代全量清单；`usage.discovery_steps` 观测。
- `task_runtime/context_assembler.py`：`snapshot.tools` 取代全量 `capabilities` 与 `capability_ids`。
- 提示词：`worker_system` 2.0.0 增加发现协议/漏选恢复/能力不足处理；`task_context` 2.0.0 说明 tools 字段。

### Observable Result
- 新增 `tests/unit/backend/test_worker_tool_injection.py` 16 例全通过。
- 全量离线：900 passed / 8 skipped（PG 专项默认跳过）；`tests/integration` 330 passed。
- 对话 Agent 仍固定 5 个任务元工具（既有用例继续通过）。

## V36 本机 Bash 常驻能力（完成）

### Implemented
- `tools/local_shell.py`：真实 bash 子进程执行器（`resolve_bash` 配置→PATH→Git Bash 路径，
  失败返回 `shell_unavailable` 不回退）；临时文件承载输出并限量读取；超时终止进程树；
  最小环境白名单（不传密钥类变量）；结构化结果（exit_code/stdout/stderr/timed_out/truncated/shell）。
- `tools/shell_policy.py`：命令静态分级（只读白名单 + 复合操作符拦截 + git 子命令细分 + 网络命令识别）。
- `tools/builtin_shell.py` + `builtin_tools.py`：注册 `run_shell`（medium / writes_external_world /
  `non_repeatable_external`），工具总数 37。
- `control/task_policy.py`：`run_shell` 只读命令放行、其余审批、网络命令需 `allow_network`；
  命令内路径必须落在 `allowed_roots`（越界 block），动态路径（`$VAR`）降级为审批。
- `runtime/policy_engine.py`：对话侧如绑定该工具，同样按命令内容要求确认。
- `task_runtime/tool_injection.py`：`run_shell` 加入 L0 常驻清单。
- 提示词 `worker_system` 2.1.1：说明 cwd 与命令内路径约束、只读/审批/联网边界、超时与截断语义。

### Observable Result
- 新增 `tests/unit/backend/test_local_shell.py` 30 例全通过（真实 bash 执行、超时清理、截断、
  环境隔离、bash 缺失不回退、分级策略、路径越界与动态路径降级、任务内直接执行与审批/路径拦截）。
- 本机实测 shell：GNU bash 5.2.26（Git Bash）。

## V37 Supervisor 进程托管（完成）

### Implemented
- `supervisor/control.py`：控制面文件协议——`state.json`、`events.jsonl`、`requests/`、`results/`、
  `processes.json`；原子写（临时文件 + fsync + `os.replace`）、`FileLock`（O_EXCL + pid 存活/超时判定）、
  幂等结果回查。`pid_alive` 在 Windows 用 OpenProcess/GetExitCodeProcess，避免 `os.kill(pid,0)` 被当成杀进程。
- `supervisor/process_control.py`：`ProcessController` 启动/停止/存活/回收；`processes.json` 为唯一所有权凭据，
  未登记 pid 拒绝操作；启动命令来自配置模板（占位符 `{python} {host} {port} {slot} {slot_root} {app_module}`）；
  POSIX 进程组 SIGTERM→SIGKILL，Windows 自有子进程 TerminateProcess 快路径、接管进程 `taskkill /T /F`。
- `supervisor/health_http.py`：运行中进程的 `/health` 探测（stdlib、不走代理），支持等待与超时。
- `supervisor/daemon.py`：`Supervisor`（recover/tick/run_forever）——暂存端口验证候选、停旧启新、
  观察窗监测、异常退出重启预算、预算耗尽回滚、`degraded` 兜底、中断切换恢复、存活进程接管与指针收敛；
  `python -m aiive.supervisor.daemon [--status|--once]` 为独立进程入口，单实例锁 + 切换锁。
- `supervisor/client.py`：应用侧客户端（提交即返回、状态查询），`supervisor_client_from_settings` 受
  `AIIVE_SUPERVISOR_ENABLED` 控制。
- `supervisor/release_manager.py`：新增 `client` 注入——启用 Supervisor 时闸门通过后只提交请求并返回
  `status=switch_requested`（不自行切指针）；`monitor_once` 在托管模式返回 `supervised`，避免双头监测。
- `api/routes_selfdev.py`：新增只读 `GET /api/selfdev/supervisor`；promote/rollback 走托管客户端并把
  请求态记为 `promote_requested`/`rollback_requested`。`worker/scheduler_daemon.py` 同步注入客户端。
- `config.py`：新增 `supervisor_*` 配置组（enabled/host/port/health_path/app_module/app_command/超时/轮询/
  重启预算/观察窗/状态目录）。
- `main.py`：新增 `AIIVE_PROCESS_ROLE=candidate` 分支——候选进程只加载应用与 `/health`，
  跳过迁移、调度器、Outbox 与 MCP 注册，避免与在运行的旧版本并发迁移或重复跑周期任务。

### 晋升顺序（失败即回滚并验证）
1. 应用侧静态健康 + `evaluate_promotion_gate`（契约 + Replay Eval + 候选评测，fail-closed）；
2. 提交请求（`gate_ok=true`），守护进程拒绝无该标记的请求；
3. 暂存端口启动候选 → HTTP 健康；进程提前退出判 `candidate_start_failed`；
4. 停旧进程 → 主端口启动候选 → 再次健康；
5. 通过后才写 `active_slot` + `state.json` + `release_state.json`（观察窗）；
6. 任一环节失败 → 停候选、重启旧槽位、**验证旧版本健康**，指针保持旧值。

### Observable Result
- 新增 `tests/unit/backend/test_supervisor_lifecycle.py` 16 例全通过（真实本地子进程）：
  启停与所有权、晋升成功（pid 变化/状态一致/事件完整）、候选启动失败、暂存健康失败、
  主端口健康失败回滚并 `verified=true`、观察窗内异常退出自动回滚、无 `gate_ok` 拒绝、
  幂等去重、并发切换拒绝、Supervisor 重启接管、中断切换恢复、manifest 数据隔离。
- 设计说明：`docs/architecture/supervisor_进程托管.md`。

## V38 能力生命周期闭环（完成）

### Implemented
- `capability/states.py`：`CapabilityState` 六态（candidate/sandbox/needs_review/verified/
  active/disabled）+ `LIFECYCLE_TRANSITIONS` 唯一转换表；`ensure_transition` 对非法转换抛
  `InvalidCapabilityTransition`（candidate→active、active→verified 等均被拒绝）。
- `capability/lifecycle.py`：`CapabilityLifecycleService` 提供 status/verify/probe/enable/
  disable/upgrade/rollback/uninstall/audit/reconcile。四个事实（installed_at/verified_at/
  registered_at/enabled_at）分开记录在 `definition["lifecycle"]`，历史有界 20 条。
- 启用：校验 launch 与沙箱产物 → 校验 `verified_fingerprint`（验证后改定义必须重验）→
  真实注册 → 才置 active；注册失败状态不变。停用：按 `registered_tools` + 缓存工具列表
  精确反注册、关闭 MCP 会话、保留 launch/trust/risk 等用户配置。
- 升级：新版本装到 `<sandbox>/<name>@<version>`，用 `#staging` 独立会话验证；验证失败只删
  staging 目录、旧定义与注册不变；切换失败恢复旧定义/旧 `descriptor_hash`/旧状态并重新注册。
  回滚：从 `capability_versions.definition_snapshot` 恢复定义、注册与状态，而非只改版本号。
- 卸载：仅 `mcp:` 能力可卸载；未终结 Action / Task 引用时返回 `capability_in_use`（`force` 才继续）；
  清理注册、会话、安装记录、版本记录、能力行与专属沙箱目录；目录必须在 `SANDBOX_ROOT` 下且
  名字等于 `sanitize(name)` 或 `sanitize(name)@*`，越界目录只告警跳过。
- 一致性：`audit()` 报告 active_not_registered / inactive_still_registered /
  missing_artifacts / orphan_registration / version_mismatch；`reconcile()` 只修注册索引与
  故障态，不动文件、不删记录。
- `control/capability_broker.py`：停用能力的在途 Action 派发返回 `capability_disabled`
  （与 `unknown_tool` 区分）。
- `mcp/installer.py`：抽出 `install_to_dir`（支持版本化目录）、`run_smoke(auto_activate=False)`
  只推进到 verified；`mcp/bootstrap.py` 新增 `registered_tool_ids` /
  `unregister_capability_tools` 作为发现索引同步的唯一入口。
- `api/routes_capabilities.py`：新增 `/api/capabilities/lifecycle/*` 九个端点；activate 链路改为
  probe(→verified) + enable(→active)，不再把验证当启用。
- DB：`capability_versions` 新增 source/state/sandbox_path/definition_snapshot 四列（迁移
  `b4d7c1e9a2f5`，全部可空，无需回填）。
- 文档：`docs/architecture/capability_生命周期.md`；CODE_MAP §2.4/§3.1/§3.7/§5 同步。

### Observable Result
- 新增 `tests/unit/backend/test_capability_lifecycle.py` 29 例全通过（全离线）：
  状态机合法性、安装≠验证≠注册≠启用、验证不启用、启用需验证、定义变更需重验、
  停用后从发现索引消失且保留配置、停用/启用/升级/卸载幂等、停用后 dispatch 返回
  `capability_disabled`、同能力写操作串行、升级成功/验证失败/注册失败回滚、
  回滚恢复定义与注册、卸载清理范围与越界保护、在途 Action 拒绝与 force、
  审计与 reconcile、历史有界。

---

## V39 知识库维护闭环（完成）

### Implemented
- `knowledge/ingestor.py`：文档五态（indexed / index_failed / source_unavailable /
  superseded / deleted）。`ingest` 版本感知：同 `source_path` 内容变化时旧版本置
  `superseded` + 写入 `superseded_by` + **立即清空旧分块**，内容回到历史版本时复活原记录。
  新增 `delete`（软删除 + 释放原文对象 + 幂等）、`purge_chunks`、`release_object`；
  `reindex` 拒绝失效文档并支持 `mark_failure=False` 供维护任务在保存点内调用。
- `search_chunks`：唯一过滤条件 `Document.status NOT IN (superseded, deleted)`，
  从根上保证"更新/删除后旧内容不可召回"，无需维护额外检索索引。
- `knowledge/maintenance.py`：`scan_knowledge_base` 报告 7 类问题
  （orphan_chunks / duplicate_chunks / stale_document_chunks / missing_chunks /
  missing_object / orphan_objects / expired_superseded_objects）；`apply_report` 逐类幂等清理，
  每篇重建走独立 SAVEPOINT，单篇失败标记 `index_failed` 不影响整批；
  `documents_rebuild_failed` 不计入"有进展"，避免维护任务无限空转。
- 所有权边界：删除只动 `documents` / `chunks` / 对象存储 `knowledge-documents` 桶，
  **永不删除 `source_path` 指向的用户文件**（响应显式 `source_file_removed: false`）；
  对象回收只处理 `sha256/xx/<64 hex>` 形态键并跳过隐藏目录。
- Outbox：新增 job_type `knowledge_maintenance`（`worker/job_types.py::KNOWLEDGE_JOB_TYPES`）
  + `worker/handlers_knowledge.py::handle_knowledge_maintenance`（5 轮扫描-清理、
  未清完 CONTINUE、无进展 COMPLETED(partial)）；
  `scheduler_daemon._knowledge_maintenance_scanner_job` 每小时检查，DB 侧不一致即时入队，
  否则每日一次对象对账，`operation_id` 按天幂等。
- API：`DELETE /api/knowledge/{id}`、`GET/POST /api/knowledge/maintenance`，
  `GET /api/knowledge` 增加 source_path / superseded_by / updated_at / deleted_at。
- DB：`documents` 新增 superseded_by / deleted_at / updated_at + `source_path` 索引，
  status 约束扩展两态；`chunks` 新增 `(document_id, chunk_index)` 唯一约束
  （迁移 `c7e3a1f5b9d2`，迁移前清理历史重复行；已验证 upgrade/downgrade 双向）。
- 文档：`docs/architecture/knowledge_维护.md`；CODE_MAP §2.5/§3.2/§3.8/§3.11 同步。

### Observable Result
- 新增 `tests/unit/backend/test_knowledge_maintenance.py` 27 例全通过（全离线，对象存储隔离到 tmp_path）：
  版本取代后旧内容零召回、内容回退复活旧记录、重复导入幂等、删除不动原文件、重复删除幂等、
  失效文档拒绝重索引、分块唯一约束、7 类问题识别、缺失分块重建、原文损坏保留 index_failed、
  原文丢失标记 source_unavailable、孤儿/过期对象回收、新写入对象不被回收、
  Handler 幂等与无进展停止、调度入队幂等、列表默认过滤墓碑、OpenAPI schema 可构建。
- 迁移 `c7e3a1f5b9d2` 在 SQLite 上 upgrade → downgrade → upgrade 全通；`alembic check`
  对 documents / chunks 无结构漂移（仅剩既有的 12 项 SQLite 类型差异）。
- 全量：1005 passed / 8 skipped / 0 failed（980 基线 + 25 新增，148s）。
- ruff `--select F,E9`：All checks passed；basedpyright 全仓：0 errors / 0 warnings / 0 notes。

> 统计口径更正（V39 独立审查轮）：上条"1005"为笔误——新增用例实际 27 例，
> 正确口径为 **1007 passed / 8 skipped / 0 failed**（980 基线 + 27 新增）。
> 审查机（Windows，WSL bash 不可用）实测 1000 passed / 8 skipped / 7 failed，
> 7 个失败全部是 `test_local_shell.py` 的 bash 环境依赖，与 V39 无关。

---

## V39 独立代码审查（审查后修复轮）

### 发现与修复
- **P1 调度幂等缺陷（已修复）**：`_knowledge_maintenance_scanner_job` 原实现对"当天已存在
  任意状态的 knowledge_maintenance 任务"直接返回，导致当天首个任务完成/失败后，新出现的
  DB 侧不一致（孤儿/失效/缺失分块、index_failed）直到次日才能入队，违背"DB 侧不一致即时
  入队"的设计约定。修复：在途判定收紧为 pending/running（与记忆维护扫描器一致），
  `enqueue_knowledge_maintenance_job` 在基础 operation_id 被终态任务占用时分配
  `#2/#3…` 后缀补跑（UNIQUE 不撞车）；回归测试 3 例。
- **P2 路由错误码契约（已修复）**：原文对象丢失/损坏时 `POST /{id}/reindex` 与
  `GET /{id}/source` 直接抛 FileNotFoundError / ValueError（HTTP 500）。修复：统一映射
  409；回归测试 2 例。
- **P2 迁移契约无自动化覆盖（已补齐）**：`test_schema_migrations.py` 新增
  `test_knowledge_lifecycle_migration_dedups_and_roundtrips`：从父修订造历史重复分块，
  验证 head 去重（保留最早一条）、新列/索引/唯一约束/状态约束、updated_at 回填非空、
  downgrade→upgrade 往返。
- **P2 并发导入同路径竞态（已根治）**：架构文档 §9 原文声称并发导入同路径
  "最终只有一个活跃版本"不成立（READ COMMITTED 下不同内容的并发插入互不可见，
  无约束拦截）。修复：新增迁移 `d1e2f3a4b5c6`，为 `documents` 建
  `ix_documents_active_source_path` 部分唯一索引（活跃三态），ingest 写序调整为
  "先置失效旧版本并 flush，再写入/激活新版本"（新文档 id 预生成以保留 superseded_by
  版本链），路由把 `IntegrityError` 映射为 409（可重试）。迁移内置历史重复活跃文档
  去重（每个 source_path 保留 created_at 最新一条，旧版分块清空）。
  回归：SQLite 侧 3 例（schema 拦截、失效态不占位、路由 409）+ 迁移契约 1 例；
  PG 并发语义 2 例已写入 `tests/integration/pg/test_pg_contracts.py`（本机无
  Docker 未执行，需在 PG 环境验证）。
- 文档口径：架构文档 §7 补充后缀补跑语义；§10 更新为 35 例；测试统计口径见上。

### 验证
- 受影响测试：`test_knowledge_maintenance.py`（35）+ `test_schema_migrations.py`（7）
  + `test_ingestor.py`（8）共 50 例全通过（离线）。
- 全量回归见最终汇报。

### Known Gaps
- 不提供"回滚到旧版本" API（`superseded` 原文对象保留 30 天，仅保留能力）。
- 对象级问题（孤儿对象 / 原文丢失）每日对账一次，不触发即时入队（需遍历文件系统）。
- 不做原文哈希全量巡检：完整性在读取路径 `get_verified` 校验。
- `deleted` 墓碑行不自动清理（体量小，保留审计）。

---

## V40 自主修复闭环（完成）

### Implemented
- `selfdev/project_reader.py`（新增）：`ProjectReader` 仓库范围内只读、有界检索/读取；
  `collect_context` 提取目标中的文件路径/标识符 → 检索 → 取命中最多文件 → 附带同名测试；
  `format_context_for_prompt` 渲染提示词片段。拒绝绝对路径/`..`/符号链接逃逸、
  `.env`/`*.pem`/`*.key`/`*.db`/`*.log`、`.git/.venv/node_modules/data/runtime/slots` 等。
  新增原因：`read_text_file` 硬限定 `~/Documents`，`run_shell` 受 TaskScope/审批约束，
  都不能用于服务端读仓库源码；未重复实现任何写文件工具。
- `selfdev/planner.py`：`plan(..., context=, previous_plan=, failure_feedback=)`，
  复用同一个 `selfdev.patch_plan` 提示词（不新增提示词条目），把只读代码与真实测试错误
  作为待规划数据注入；原有单参数调用完全兼容。
- `selfdev/repair_loop.py`（新增）：`SelfDevRepairLoop.run` 串起
  读码 → 规划 → `PatchExecutor.apply_to_inactive` → `TargetedTestRunner` →
  失败时按真实错误有限修复（默认 3 轮，硬上限 5）→ `evaluate_promotion_gate` →
  可选 `ReleaseManager.promote()`。
  `patch_fingerprint` 对相同补丁提前停止；所有失败状态显式（`planner_failed` /
  `blocked` / `apply_failed` / `tests_failed` / `gate_failed` / `promote_failed`），
  绝不伪造成功。`persist_repair_run` 落库到 `SelfDevRequest.plan["repair"]` + `PatchOperation`。
- `selfdev/repair_guard.py`（新增）：补并发控制、候选隔离、中断恢复三件事，不重复版本管理与晋升。
  `RepairLock`：`O_CREAT|O_EXCL` 原子建锁 + owner token，只有持有者能释放、持有者存活时绝不抢占、
  死进程陈旧锁可接管，占用时返回 `repair_busy`。`CandidateSlotGuard`：改写前记活跃槽位基线指纹并
  `mark_not_promotable`，失败整树 `restore` 并复核指纹，恢复不了就隔离为不可晋升。
  `RepairJournal` + `recover_interrupted`：阶段快照落 `runtime/selfdev/runs/<run_id>.json`（终态保留 7 天），
  下次运行前按日志回滚上次中断，状态只写 `interrupted*`。
  `RepairLoop.run` 改为：取锁 → 恢复中断 → 闭环；`ReleaseManager._candidate_block` 晋升前核对
  不可晋升标记与指纹；手工 `apply-inactive` 同样受锁与标记约束。
- 安全边界：本模块没有直接写文件/执行 shell 的代码；写入只经 `PatchExecutor`
  （非活跃槽位 + Trusted Core + 路径穿越 + safe_delete 四层校验），晋升只经
  `ReleaseManager`（健康检查 + 安全门 + Supervisor + 观察窗回滚）。
- API/工具：`POST /api/selfdev/repair`（Trusted Core token 守卫）、
  工具 `run_selfdev_repair`（`high` 风险 + 需确认，`promote` 默认 false）。
- 文档：`docs/architecture/selfdev_自主修复.md`；CODE_MAP §2.4/§2.8/§3.1/§3.7 同步。

### Observable Result
- 新增 `tests/unit/backend/test_selfdev_repair.py` 28 例全通过（全离线，LLM/测试运行器用替身）：
  补丁指纹稳定性与敏感性、一轮修复成功、轮次耗尽失败、相同补丁提前停止、阻塞计划不应用、
  应用失败、规划器异常、安全门失败/异常 fail-closed、晋升请求/失败/禁用、
  真实 `PatchExecutor` 只改非活跃槽位、ProjectReader 检索与拒绝边界、
  Planner 上下文/错误注入、持久化与路由/工具接线。
- 新增 `tests/unit/backend/test_selfdev_repair_guard.py` 25 例全通过（全离线）：锁互斥/陈旧接管/
  越权释放、三类失败后候选回滚与恢复失败隔离、安全门后候选被改即阻断、三方指纹绑定、
  journal 阶段与终态、中断恢复六种终态与幂等、晋升前标记核对、手工 apply 的锁与标记约束。
- 回归：`test_selfdev_planner.py`、`test_patch_executor.py`、`test_targeted_test_runner.py`、
  `test_prompt_registry.py`、`test_identity_memory.py`、`test_tool_registry.py` 共 55 例全通过
  （清理后复跑 108 例仍全过）。
- 全量离线：`1070 passed / 10 skipped / 0 failed`（163.31s；10 skipped 为 PG 专项默认未启用）。
- ruff `--select F,E9` 改动文件 All checks passed；新增文件默认规则亦全过；
  basedpyright `backend`：0 errors / 0 warnings / 0 notes（tests 不在类型检查基线内）。

### Known Gaps
- 不做语义级代码理解（无 AST/调用图）：ProjectReader 是子串检索 + 文件读取。
- LLM 生成完整文件内容而非 diff，大文件 token 成本高；靠 40KB/120KB 上限兜底。
- 定向测试依赖 `TEST_MAPPING`；未登记目录会 fail-closed，需要人工补映射。
- 默认不自动晋升（`promote=False`）；自动晋升需显式开启，且仍受 ReleaseManager 闸门约束。
- 并发锁是**本地文件锁**：只约束同一 runtime 目录下的进程；多实例/跨机器并发不覆盖。
- 中断恢复只按 journal 回滚候选槽位，不重放中断时的规划结果（下次从零重新规划）。
- 候选隔离依赖 `slots/<X>/candidate_state.json` 与指纹；runtime 目录被整体清空则失去保护。
