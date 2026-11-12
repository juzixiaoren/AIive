# Current Phase

## 版本索引
- 当前工作：**P1/P2 收尾：状态一致性、附件 Forget 与资源安全**（2026-11-12），详见文末 P1 记录与验收报告；历史版本保留原记录。
- 历史版本：V62.4 / V62.5 / V62.6.1 依次见文末。
- 历史阶段：V34 → V54 按时间顺序记录在下方各节；下方 `## Phase` / `## Status` 是 V34 阶段的原始记录，不再代表当前主干。

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
  `patch_fingerprint` 对相同补丁提前停止；所有终态显式（`planner_failed` / `blocked` /
  `apply_failed` / `tests_failed` / `gate_failed` / `promote_failed` / `candidate_changed` /
  `repair_busy` / `failed`），
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
- 文档：`docs/architecture/selfdev_自主修复.md`；CODE_MAP §2.4/§3.2/§3.6/§3.7 同步。

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
- 候选隔离依赖 `slots/<X>/candidate_state.json` 与指纹（在 `slots/` 下，不在 runtime）；
  `runtime/` 被清空只丢 journal 与锁，不可晋升标记仍在，等下一次修复结束才解除。

---

## V41 时序知识图谱（Temporal KG，完成）

### Implemented
- 提取（复用同一次 LLM 调用）：`prompts/memory/extractor.md` 增加可选 `graph_facts`（subject/predicate/
  object/confidence/source_span/valid_from/valid_to）；`ExtractedMemory` 用宽松 `GraphFactPayload` 接住，
  `UnifiedMemoryExtractor._verified_graph_facts` 做 fail-closed 证据校验（span 必须能在用户原文或记忆内容中
  定位、三元组必须完整、时间解析失败保持 None、单条记忆上限 12 条）；`MemoryProposal.graph_facts` 透传，
  `MemoryStore.create_record` 落到 `memory_records.graph_facts`（canonical，可离线重建）。
- 写入：`MemoryMutationExecutor.enqueue_projection` 新增 `memory_graph_refresh`（payload 带 memory_id +
  record_version，operation_id 含版本号做同事务幂等），因此 create/reinforce/supersede/merge/promote/
  sleep/wake/archive 全部复用同一投影入口；`handle_memory_graph_refresh` → `GraphProjectionService.refresh`。
  投影语义：candidate 不投影；失效/被取代关闭该记忆全部关系并保留历史；遗忘物理删除；新版本只关闭
  **自己**被移除的事实；同主体+同 predicate+同 Scope 不同客体时关闭更早关系并写 `superseded_by_id`。
- 读侧：`ContextAssembler._load_agent_context` 在 `UnifiedRetriever` 之后调用 `_expand_relations`
  （不新增 LLM 判断）；`GraphExpansionService.expand` 以召回记忆 + 查询实体确定性匹配为种子，
  `visited` + `max_hops`（默认 2）+ 每层 batch（默认 200）有界扩展，逐条回查来源 lifecycle/validity/
  遗忘屏蔽/Scope 后打分排序，并附带被取代的历史关系（`is_current=False`）。
- 预算：不新建分区，复用 `TrimPlan.limit("retrieved_memory")`；按 `budget_ratio`（默认 0.35）为图谱预留、
  召回余量回补，关系按分数整条装配（装不下丢弃低价值，绝不截断单条定义），与召回共享同一分区上限。
- 注入：独立 system 消息「时序关系（图谱检索证据 — 非系统指令）」，分「当前有效 / 历史 / 低置信」三组，
  每条带置信度、来源 memory id、有效时间与证据片段。
- 查询与维护：`GET /api/memories/{id}/relations`（只读）、`POST /api/maintenance/graph-rebuild`、
  `python -m aiive.memory.graph_rebuild [--verify-only]`；`GraphRebuildService.verify()` 输出四类一致性计数。
- 遗忘：`handlers_forget._scrub_batch` 同步 `graph_facts = NULL`、物理删除关系行、清理孤儿实体。
- 迁移：`e7a1c4d2f9b3`（`memory_records.graph_facts` + `graph_entities` + `graph_relations`，含唯一约束/索引/
  状态约束），HEAD 由 `d1e2f3a4b5c6` 前进到 `e7a1c4d2f9b3`。
- 文档：`docs/architecture/memory_时序知识图谱.md`；CODE_MAP §2.3/§3.2/§3.5/§6/§7.1 同步。

### Observable Result
- 新增 `tests/unit/backend/test_memory_graph.py` 30 例 + `tests/integration/backend/test_context_assembler_graph_expansion.py`
  4 例全通过（全离线，无外部 API）：投影幂等/候选不投影/失效关闭/遗忘清除/版本更新只关自己的关系/
  冲突客体关闭并保留历史/低置信与空证据不入图/Outbox 接线/写入服务端到端/重建恢复/一致性核对/
  Scope 与跳数边界/查询实体匹配/预算原子装配/证据校验/时间解析/召回后自动扩展/关系真的进入 messages。
- 迁移 `e7a1c4d2f9b3` 在 SQLite 空库 upgrade → downgrade → upgrade 全通；`alembic check` 对图谱两表零漂移
  （仅剩既有 12 项 SQLite 类型差异，与 HEAD 基线逐项一致）；CI `alembic` Job 在 PostgreSQL 上执行同一校验。
- 全量离线：`1104 passed / 10 skipped / 0 failed`（186.33s；10 skipped 为 PG 专项默认未启用）。
- ruff `--select F,E9` 改动文件 All checks passed；basedpyright（memory/、context_assembler、routes_memories、
  outbox_handlers、handlers_forget、db/models）0 errors / 0 warnings / 0 notes。

### Known Gaps
- 实体规范化仅字符串归一，不做同义词/指代消解；"AIive" 与 "AIive 项目" 视为不同实体。
- 关系覆盖率取决于模型是否主动输出 `graph_facts`（提示词宁缺毋滥，不强制）。
- 冲突关闭只按（主体, predicate, Scope, 客体）判定，不理解多值 predicate 的业务语义。
  （V42 已修正：单值/多值区分 + 记忆层权威取代，见下节。）
- 图谱不参与 RRF 融合，不改变普通召回排序；两者只在上下文预算层协调。

## V42 关系基数与冲突处理修正（完成）

### Implemented

- 新增 `memory/graph_predicates.py`：谓词基数配置（单值谓词表 + 别名族 + settings 扩展）。
  `cardinality` / `is_single_valued` / `predicate_family` / `conflicted_group_keys`；
  未知谓词一律按多值处理（宁可并存，不误删）。
- 投影（`graph_projection.py`）：`_close_conflicts` 改为两道闸门 —— 谓词显式单值 **且**
  记忆层权威取代（旧记录 validity 失效 + `superseded_by` 指向本条记忆，新增
  `has_authoritative_supersede`）；多值/未知谓词永不因新事实关闭旧关系；同一条记忆内部
  不同客体不在投影层裁决。
- 终态区分：`fact_removed` / `no_facts` / `not_accepted` → `status=retracted`（不写 `valid_to`）；
  来源 validity 失效 → `closed` + `record_*`；被取代 → `closed` + `superseded_by_newer_fact`
  + `superseded_by_id` + `valid_to`；`sleeping` 保持 `active`（读侧默认不注入，唤醒即恢复）。
- 读侧（`graph_expansion.py`）：`mark_single_valued_conflicts` + `_mark_conflicts`（对窗口内单值关系
  再做一次同主体有界批查询，避免另一侧不在 BFS 结果时把单条当成确定事实）；
  `GraphRelationView` 增加 `cardinality` / `is_conflicted`，`GraphExpansionResult.conflicted`；
  注入分组改为 当前有效 → 冲突 → 历史 → 低置信。
- 查询（`routes_memories.py`）：关系附带 `cardinality` 与 `conflicted`（按同主体批查询跨记忆判定）。
- 修复（`graph_rebuild.py`）：`repair_overclosed(dry_run=)` 只恢复有确定依据的误关；`rebuild()` 先修复
  再投影；`verify()` 增加 `conflicting_single_valued_groups` / `suspect_closed_relations`；
  CLI 增加 `--repair-only` / `--dry-run`。
- 配置：`AIIVE_MEMORY_GRAPH_SINGLE_VALUED_PREDICATES`（逗号分隔）扩展业务单值谓词，无需改代码。

### Observable Result

- `tests/unit/backend/test_memory_graph.py` 46 例（原 30 例，新增/改写 16 例）+ 集成 4 例全通过：
  单值+权威取代才关闭并保留历史、单值无权威并存并标冲突、多值/未知谓词并存、谓词族别名同规则、
  配置扩展单值谓词、窗口外冲突仍被发现、重复投影不关闭并存关系、撤回与关闭两种终态、
  旧规则误关修复（多值恢复 / 单值来源仍有效恢复 / 权威取代保留 / 事实已删除不恢复 / dry-run 不写库 / 幂等）。
- 全量离线：`1120 passed / 10 skipped / 0 failed`（155.37s；10 skipped 为 PG 专项默认未启用）。
- ruff `--select F,E9` 改动文件 All checks passed；basedpyright（graph_predicates / graph_projection /
  graph_expansion / graph_rebuild / graph_types / routes_memories / config）0 errors / 0 warnings / 0 notes。

### Known Gaps

- 单值谓词表是人工维护的保守清单；新增业务谓词需显式声明，否则按多值处理（可能少关旧关系，不会误删）。
- 谓词别名只做字符串族匹配，不做语义或多语言归一；中文谓词仍会被 `normalize_predicate` 丢弃，
  属既有提取约束，本次未改。
- 冲突只做「同主体 + 谓词族 + Scope」级别提示，不校验两条关系的时间区间是否真的重叠。
- 历史数据修复只覆盖 `closed_reason=superseded_by_newer_fact` 的旧规则产物；更早的其他关闭原因
  没有对应规则，保持原样。

## V43 长期记忆冲突待确认闭环（完成）

### Implemented

- 新增 `memory/conflict_candidates.py`：写入前冲突候选发现，三路合并 —— 精确命中
  （`canonical_key` + Scope，lifecycle ∈ active/sleeping 且 validity = valid）、
  语义召回（复用 `UnifiedRetriever` AUTO，遵守既有 keyword/vector/hybrid 配置，降级只记 note 不报错）、
  图谱关系（`graph_facts` → 实体归一 + 有界别名扫描 → 同主体/谓词族/Scope 关系反查来源记忆）。
  合并去重后经 `ForgetVisibilityService.filter_memory_ids` 做 fail-closed 可见性校验；
  `content_hash` 相同者剔除。召回未命中不会被当作"无冲突"。
- 新增 `memory/conflict_adjudication.py`：语义裁决与决策映射。六种结论
  （compatible / complementary / conditional / temporal_change / contradiction / uncertain）
  → 九种决策（keep_old / accept_new / revise / merge / keep_both / reject_new / forget_old / clarify / unrelated）
  → 两种动作（proceed / defer）。只有"单值谓词 + 双方可信"的 contradiction 才允许直接取代；
  conditional 在单值场景下同样转人工；uncertain 一律 defer；LLM 不可用时降级 proceed（不阻塞主链路）。
  同时提供澄清问题生成与自然语言回复解析（带确定性兜底）。
- 新增 `memory/conflict_confirmation.py`：`MemoryConflictService` 编排
  `collect_candidates`（只读会话）/ `decide`（无会话，事务外 LLM）/ `park`（只写案例 + 入队问题 job）/
  `publish_question`（行锁 + TTL + 通知幂等，pending → asking）/ `enqueue_reply_jobs`（按 case + turn 幂等）/
  `apply_decision`（行锁 + 状态/TTL/版本/active 复核 → 执行或判 stale）。
  决策执行只经 `MemoryWriteService.write(decision=..., decision_target_id=...)` 或 `forget`，
  LLM 不直接改库。clarify 轮次受 `AIIVE_MEMORY_CONFLICT_MAX_CLARIFY_ROUNDS` 限制。
- 新增表 `memory_conflict_cases`（迁移 `b7f3c1d9a4e2`）：冲突双方 id/版本、提案快照、
  裁决结论与证据、图谱证据、澄清问题、决策与结果；`idempotency_key` 唯一；
  状态机 pending / asking / resolved / rejected / stale / expired。
- 接线：`handle_memory_extraction` 新增 Phase B2（候选发现 + 裁决），Phase C 传 `conflict_plan` 给
  `write_batch`；`defer` 的提案在事务内只落案例与 `memory_proposals(conflict_pending)`，
  不写 `memory_records`、不写关系投影。`turn_execution._resolve_and_preempt` 在用户消息事件同事务内
  入队 `memory_conflict_reply`。新增 `worker/handlers_conflict.py` 两个 Handler
  （短事务读快照 → 事务外 LLM → 短事务落库），并在 `register_all` 注册（schema_version=1）。
- 读侧：`GET /api/memories/conflicts` 只暴露案例状态、双方内容（经 `MemoryPolicyEngine.render_content`
  按敏感级渲染）、问题、轮次与决策，不暴露内部模型调用。
- 配置：`AIIVE_MEMORY_CONFLICT_CONFIRMATION_ENABLED` / `_MAX_CANDIDATES` /
  `_RETRIEVAL_MIN_SCORE` / `_TTL_HOURS` / `_MAX_CLARIFY_ROUNDS`。
- 图谱一致性：accept/revise/merge 后仅由正式落库记录 + 其 `graph_facts` 经既有 Outbox
  `memory_graph_refresh` → `GraphProjectionService.refresh` 更新；旧关系只在"谓词显式单值 +
  记忆层权威取代（`has_authoritative_supersede`）"时关闭并留历史；未知谓词按多值处理，永不因新关系关闭旧关系。
  reject_new / keep_old 不改关系；forget 走既有 Forget Saga，遗忘事实不会被图谱扩展召回。
- 过期清扫：`scheduler_daemon._conflict_expiry_scanner_job`（15 分钟轮询、按小时窗口幂等入队）
  → outbox `memory_conflict_cleanup` → `MemoryConflictService.expire_open_cases`
  （行锁 + 批量 200，超出则 CONTINUE 分页）：把 TTL 已过的 pending/asking 案例置为
  `expired` / `ttl_expired`，并把该案例的通知置为 `cancelled`；只改案例与通知，
  不写记忆、不写关系、不触发投影。

### Observable Result

- `tests/integration/backend/test_memory_conflict_confirmation.py` 30 例 + `tests/unit/backend/test_memory_conflict_adjudication.py` 19 例全通过：
  三路候选发现、pending 期不污染记忆与图谱、六类回复决策、跨 key 取代、keep_both 基数闸门、
  forget 后图谱扩展不可见、重复提案/重复通知/回复重放/版本过期/TTL 过期。
- 全量离线：`1161 passed / 10 skipped / 0 failed`（160.04s；10 skipped 为 PG 专项默认未启用）。
- ruff `--select F,E9` All checks passed；basedpyright `backend/aiive` 0 errors / 0 warnings / 0 notes。

### Known Gaps

- 裁决质量取决于 LLM 判断；证据不足时转人工是设计选择，会带来确认负担。
- 候选发现依赖 `graph_facts` 与实体名归一，实体别名覆盖不足时会漏掉图谱路候选（精确命中与语义召回仍在）。
- 澄清轮次上限（默认 3）后不再追问，案例停留在 asking 直到 TTL 过期（默认 72h）。
- `keep_both` 对单值谓词一律拒绝，即使两条事实其实描述不同时间段；需用户改用 revise/merge。

## V44 长期记忆图谱的实体绑定（完成）

### Implemented

- 复用现有候选：`ConflictCandidateFinder._collect_entities` 在候选发现阶段同时收集实体候选
  （`EntityCandidate`）：来源包括本次冲突证据里已出现的关系两端实体，以及同 Scope 内有界扫描
  命中的实体；匹配依据只用来决定"要不要给模型看"（精确名 / 别名 / 词重叠 / 字符串包含 / 关系端点），
  不据此合并。名称已被精确名或已确认别名命中时直接产确定性绑定（`resolved_entity_bindings`），
  不占用模型判断。实体候选严格限制在与记忆相同的 Scope（实体锚点按 Scope 唯一）。
- 同一次 LLM 调用：`SemanticConflictAdjudicator.adjudicate` 的输出在原 verdict 之外增加
  `entity_bindings`（reuse / create_new / unresolved），提示词给出待绑定名称、关系事实、实体候选
  （含 ID、别名、Scope、候选依据），并明确要求"名称包含/相似度不是身份依据"。没有记忆冲突但存在
  实体消歧需求时也走这一次调用（不再新增独立调用）。
- 服务端校验：`validate_entity_bindings` 拒绝编造 ID（必须属于本次候选集合）、越界名称（必须属于
  待绑定集合）、不属于本次证据的 evidence ID，重复名称只保留第一条；任何不满足都降级为 unresolved，
  不回退成按名称强制合并。`apply_entity_bindings` 只把已通过的 reuse 回填到 `graph_facts`。
- 持久化：绑定写在 `GraphFact.subject_entity_id` / `object_entity_id`，随 canonical 记录落在
  `memory_records.graph_facts`（JSON 列），不需要新表、新列或迁移；`conflict_pending` 阶段随
  `memory_conflict_cases.proposal_payload` 快照保存，用户确认后重建提案即带回绑定，不再调用 LLM。
- 投影：`GraphProjectionService._resolve_entity` 优先复用绑定，但必须复核实体仍存在且 Scope 与
  记录一致；失败则退回按名称解析（安全的新建路径），并把新写法追加进实体 `aliases`，后续同义名称
  可直接确定性复用。`relation_key` 仍由三元组决定，重复投影不会产生重复关系或实体。
- 重建：`GraphRebuildService.rebuild` 逐条走 `refresh`，绑定随 canonical 记录一起恢复。

### Observable Result

- `tests/unit/backend/test_memory_entity_binding.py` 12 例：校验矩阵（编造 ID / 越界名称 / 重复名称 /
  未提供的证据 ID / create_new / 无法解析条目）、回填（主体+客体、跳过未通过项）、单次调用返回
  verdict + bindings、提示词包含候选 ID 与名称、降级无绑定。
- `tests/integration/backend/test_memory_entity_binding.py` 11 例：AIive / AIive 项目 复用同一实体、
  仅字符串包含时不合并、多候选歧义保持 unresolved、编造 ID 被拒、跨 Scope 实体不进候选、实体被删除后
  退回按名称、conflict_pending 期间不写图谱且确认后复用绑定、投影幂等、冲突与绑定共用一次调用、
  别名后续免调用、重建保留绑定。
- 记忆相关回归：`122 passed`（冲突 30 + 裁决 19 + 图谱 46 + 上下文扩展 4 + 绑定 23）。
- 全量离线：见交付报告；`basedpyright backend/aiive` 0 errors / 0 warnings / 0 notes。

### Known Gaps

- 实体候选的别名/包含匹配是有界扫描（上限 300 个实体），稠密 Scope 下可能漏候选，此时按名称新建。
- 绑定以 canonical 事实为单位；同一名称在不同记忆里若一次被判为不同实体，会各自建节点（不做全局别名表，
  这是刻意的边界）。
- 实体被遗忘级联删除后，旧绑定失效并退回按名称解析，历史记忆不会自动重新绑定。

### V44.1 名称歧义审查与修正

- 发现并修正三处边界：
  1. **别名多命中**：`_find_entity` 原先返回第一个别名匹配（依赖数据库返回顺序）。改为
     `_find_entities` 返回全部匹配，>1 视为歧义：不做确定性绑定、名称进入待绑定集合、
     记 `entity_name_ambiguous`，由同一次裁决在候选里判断；`_collect_graph` 对歧义主体同样跳过。
  2. **同一名称多次出现**：绑定原为纯名称键，无法表达"两条事实指向不同实体"。`EntityBinding`
     增加可选 `fact_index` + `role`（0 基，与提示词 `[F0]` 对应）；名称级绑定作用于全部出现，
     事实级只作用于指定位置，并校验该位置上的名称与绑定名称一致；两者并存时事实级优先。
  3. **重复/矛盾输出**：同一目标（名称级或事实级）出现不同 decision 或不同 entity_id 时整条作废
     （`conflicting_bindings`，清空 entity_id），结果与数组顺序无关；完全相同的重复条目去重。
- 无新增 LLM 调用、无新表/新列、无迁移。
- 测试：unit 22 例、integration 14 例；记忆相关回归 135 例；全量见交付报告。

### V45 运行证据驱动的改进候选发现

- 目标：补齐 `SelfDevRepairLoop` 的上游——系统如何从真实运行结果里发现下一件值得改进的事。
- 证据：`selfdev/improvement_evidence.py` 只读采集任务终态（failed/partial）、Action 失败、
  Outbox 死信、自进化修复失败、Eval 安全门失败；按"能力 + 归一化错误签名"聚合，
  单次出现、单次瞬态故障（超时/连接重置/限流）、超出组上限的噪声在进入模型前被丢弃。
  不扫描聊天原文；用户纠正只在已有结构化记录时才进入证据。
- 生成：`selfdev/improvement_discovery.py` 用 `Event(improvement_discovery_run)` 水位做进度记录；
  没有新证据不调用模型；有证据时一次 `chat(json_mode)` 产出不超过 3 个候选；
  服务端校验证据 ID 必须属于本次输入、occurrences 不得超过证据支撑、验收标准非空，
  否则整条候选作废并记录原因；同一证据组只保留一个候选。
- 持久化：复用 `selfdev_requests`（**不新增表**）：`id = uuid5(dedupe_key)` 天然幂等，
  `status` 用 `candidate_pending/accepted/rejected` 与修复运行状态区分，正文与证据存 `plan`。
  候选落库同时写 `notification_created`（category=`selfdev_improvement`），复用现有通知读模型。
- 调度：`improvement_discovery` 进入 Outbox allowlist 与 HandlerRegistry；
  `_improvement_discovery_scanner_job` 每 6 小时按窗口 `operation_id` 幂等入队，
  Handler 遵循"短事务读 → 事务外模型 → 短事务写"。
- 接受：`POST /api/selfdev/candidates/{id}/accept` 需要 trusted core token，
  固定 `promote=False` 调用既有 `SelfDevRepairLoop`，安全门、候选槽位与回滚全部沿用；
  `reject` 只改候选状态。默认不晋升、不修改活跃槽位、不新增任务引擎。
- 测试：unit 33 例（证据 12 + 发现 21）、integration 8 例，全部离线。

### V47 长期目标规划与事件驱动的持续执行

- 目标：让系统能从自然语言识别长期目标，产出可确认的结构化计划，并借助既有任务运行时持续推进，
  在用户沉默、错过、受阻时给出确定性处理。
- 复用优先（无新表 / 无新引擎）：目标就是一条 `AgentTask(task_type="long_term_goal")`，
  `task_state["goal"]` 存 `GoalPlan`（步骤 id/kind/status/depends_on/acceptance/schedule/evidence_refs/
  attempts/missed_count/next_trigger_at/approved_at），`task_state["plan"]` 存投影，
  Checkpoint 保存 plan 快照；触发复用 `AgentTaskWatch`（time，config 带 goal_step_id/occurrence/notify_only），
  执行复用 `PersistentTaskRuntime` + Outbox `agent_task_run`，通知复用 `Event(notification_created)`。
- 生命周期：`delegate_task(goal_plan)` 只提案（`blocked_user` + `goal_proposed`，确认前不产生 run）；
  `confirm` 后由 `_advance` 决定入队或停在等待态；步骤完成必须带 Evidence，
  缺证据即 `failed` + 目标 `blocked` + 通知；全部完成且证据齐才 `completed`。
- 步骤类型：`autonomous` 到期自动执行；`interactive` 到期只写通知，用户点击开始后在独立任务上下文执行；
  `approval_required` 需 `approve_step` 审批（`approved_at`）后才允许起跑，工具级审批仍走既有 `TaskApprovalService`。
- 沉默策略：未确认计划 24h 提醒一次、7 天后休眠；等待用户步骤 24h 提醒一次；周期交互场次未开始记
  `missed` 且不重启同一 occurrence；连续 3 次发“是否调整”通知，绝不自动改计划；暂停/结束取消全部 watch。
- 调度：`scheduler_daemon._agent_task_scanner_job` 追加 `scan_due_reminders()`，只处理 `wake_at <= now`，
  不扫描全部目标、不调用 LLM。
- 控制面：`GET /api/agent-tasks/{id}/goal`；`POST .../goal/{confirm,reject,pause,resume,end,modify}`；
  `POST .../goal/steps/{sid}/{start,approve}`。任务中心新增“长期目标”面板（进度、步骤状态、审批/开始、生命周期按钮）。
- 配置：`aiive_goal_proposal_reminder_hours`、`aiive_goal_proposal_expire_days`、
  `aiive_goal_wait_reminder_hours`、`aiive_goal_missed_streak_threshold`。
- 测试：`tests/unit/backend/test_long_term_goal.py` 20 例（Fake LLM，全离线），覆盖确认前不执行/只启动一次、
  步骤推进与证据校验、等待只阻塞依赖、定时触发幂等、交互只通知、独立上下文、重启恢复、
  暂停/修改/结束失效、预算耗尽、连续错过、未确认提醒、REST 控制面、Outbox 真实调用链、审批与交互语义。
- 遗留：步骤级审批未复用 `ApprovalRequest`（该表要求 action_id），审批列表页看不到目标步骤审批；
  计划修改以 schedule 是否变化判定“步骤未变”。

### V48 长期目标修改一致性与审批有效性

- 目标：修掉“修改计划后旧状态/旧证据/旧审批被错误保留”的漏洞，并核实审批的权限、持久化、审计与幂等。
- 修改语义（`GoalService.modify`）：把**执行内容**（title/kind/acceptance/depends_on/requires_evidence）
  与**调度时间**（schedule）分开判定。内容变化 → 步骤回 pending、清空 `evidence_refs` 与 `approved_at`、
  清触发与 `last_result`，note 记 `content_changed`；依赖图递归到不动点，下游记 `dependency_changed:<dep>`。
  仅改时间 → 已完成步骤保留 done 与证据，未完成步骤重新排期。
- 审批有效性：内容变化即作废旧审批，步骤回到 `waiting_user` 并重发 `goal_step_approval`；
  未重新审批不建 run，排队中的 run 也会因无可用步骤停在 `blocked_user`。重复审批抛 `step_already_approved`
  且不重复入队。`approve_step` 补写 `goal_step_approved` checkpoint（审计/回放）。
- 顺序约束：`approve_step`/`start_step`/`on_run_started` 都校验 `dependencies_met`，审批不能绕过依赖；
  运行中（dispatching/running/verifying/reconciling）拒绝修改（`goal_running`），避免结算与计划错位。
- 审计：`goal_modified` 事件带 `added / rescheduled / invalidated[{step_id,reason}] / approval_invalidated`。
- 测试：`test_long_term_goal.py` 增至 27 例，新增内容修改但时间不变、仅改时间、依赖变化、旧证据失效、
  旧审批失效、重复审批、审批依赖校验、运行中拒绝修改共 7 例。
- 数据库：无表结构变更、无迁移；`alembic check` 在本机 pgvector/pg16 容器上无 drift，
  另用事务内真实 PG 校验确认 JSON 计划与审批状态读写正确（回滚，不留数据）。

### V50 目标步骤触发规则与触发决策闭环

- 目标：一个长期目标可包含多个子任务，每个子任务带**独立触发规则**；规则命中后经确定性校验 →
  Outbox → 复用现有 LLM 产出结构化决策 → 服务端复核执行，并能主动向用户发送真实助手消息。
- 复用优先（无新表、无新引擎）：计划仍存 `task_state["goal"]`，触发复用 `AgentTaskEvent`（幂等键）+
  `OutboxJob`（`operation_id` 唯一）+ HandlerRegistry；执行复用 `GoalService`/`PersistentTaskRuntime`；
  主动消息复用 `TurnRecord` + `Event(llm_response)`（唯一聊天时间线）；Trace 复用
  `ContextSnapshot`/`LLMCall`/`Event`。
- 触发规则（`task_runtime/goal_plan.py::TriggerRule` + `goal_triggers.py` 纯函数求值）：
  `once_at`（定点一次）、`recurring`（daily/weekly + 时区 + until）、`window_once`（时间窗内条件首次满足，
  到期未满足 → MISSED + `expired_at`）、`event_condition`（事件类型 + payload 子集）、
  `opportunistic`（仅在相关事件或已定义评估时机触发，受 `cooldown_hours`/`max_per_day`/`max_per_week` 约束）。
  旧 `schedule` 经 `trigger_of` 归一化为 `TriggerRule`，历史计划与测试零改动。
- 幂等与恢复：每个触发有稳定键（`once_at:<iso>` / `recurring:<iso>` / `window_once:<start>:<event_id>` /
  `event_condition:<event_id>` / `opportunistic:<event_id>`），落 `AgentTaskEvent(dedupe_key)` +
  `OutboxJob(operation_id=goal_trigger:<task>:<step>:<sha1(key)[:16]>, trace_id=uuid5(operation_id))`；
  步骤上的 `last_trigger_key`/`trigger_count`/`armed_at` 让扫描在重启后从计划状态恢复，不重复触发。
- 决策闭环：`goal_decision.py::GoalDecisionEngine` 收集目标/步骤/依赖/触发原因/证据/相关记忆/近期对话/当前时间 →
  `chat(json_mode)` → `GoalDecision`（extra=forbid，7 种 action）→ `validate`（权限/依赖/步骤状态/审批/证据）→
  `apply`（先校验 `last_trigger_key`，过期返回 `stale_trigger`）→ 调 `GoalService` 的
  `begin_step_from_trigger` / `mark_step_waiting_user` / `save_plan` / `complete_goal`。LLM 不直接写库、不绕过安全内核。
- 主动消息：`runtime/proactive_message.py::ProactiveMessageService.post` 写 `TurnRecord(source=runtime_event,
  status=completed, turn_sequence=peek_next_turn_sequence)` + `Event(llm_response, payload.proactive=True)`；
  前台 Turn 持活跃租约时抛 `ProactiveBlocked` → Handler 返回 `RETRY_LATER`，由 Outbox 重试后按序补发，避免乱序/丢失。
- 可见性边界：`ThreadState.list_thread_messages_page` 只渲染用户可见事件，system 提示词/内部决策指令不进聊天；
  `ContextSnapshot.context_items` 保留完整 system + 历史 + 触发事件，`/api/debug/traces/{id}` 可查。
- 调度与配额：`scheduler_daemon._agent_task_scanner_job`（10s）追加 `GoalTriggerService(db).scan()`，
  只做确定性匹配（无事件不调 LLM）；Handler 侧 `aiive_goal_trigger_max_decisions_per_day`（默认 12）为硬上限。
- 用户沉默：`wait_user`/`ask_user` 只把**该步骤**置 `waiting_user`，依赖它的步骤阻塞，独立步骤照常触发。
- 测试：`tests/unit/backend/test_goal_triggers.py` 19 例（Fake LLM，全离线）：五类规则 + 时区 + 重复事件 +
  重启恢复、no_action/run_step/modify_plan/complete_goal/ask_user/send_message 决策、用户沉默、主动消息插入与
  前台 Turn 占用重试、聊天不泄露 system 提示词而 Trace 保留完整上下文、旧 schedule 归一化、
  window_once 同窗口不重复触发、显式规则不被旧时间路径起跑、modify 保留触发状态。
- 提交后复审（见交付报告第 11 节）修掉 8 处逻辑错误与 2 处边界缺失：窗口重复触发、旧时间路径与触发规则
  双启动、modify 重置触发状态、主动消息先于校验发出、step-done 评估时机被黑名单屏蔽、过期/触发未落计划、
  决策审计幂等键吞掉接受记录、trigger_count 虚增；并清理 7 处死代码/重复实现（含日历排期统一到
  `next_time_trigger`、Event 统一走 `EventLogger`）。
- 全量回归：1328 passed / 23 skipped（PG 与本地 Embedding 专项跳过）。
- 遗留风险：`event_condition`/`opportunistic` 的事件来源限定在线程 `Event` 与 `goal_step_completed` 任务事件，
  其他来源需显式接入；`window_once` 过期只置 MISSED，不会自动改计划；`recurring` 仅支持 daily/weekly
  （无 cron 表达式）；主动消息与前台 Turn 的冲突用 Outbox 重试解决，未做用户级“稍后再说”抑制。

### V53 通用计划事件源扩展
- 目标：让长期目标响应**任务执行、知识维护、自进化修复**等真实运行事件，仍由既有事件/Outbox/计划规则/
  主动消息闭环决定下一步，不新建事件总线或任务引擎。
- 事件生产（都发生在业务写入的同一事务里）：
  `task_runtime/repository.py::transition_task` 终态 → `publish_task_terminal_event`（`task_completed` /
  `task_cancelled`；目标自身任务不发布，避免自激）；`worker/handlers_knowledge.py` 维护结束 →
  `knowledge_maintenance_completed`（healthy/partial），仅最后一次重试失败 → `knowledge_maintenance_failed`；
  `selfdev/repair_loop.py::persist_repair_run` → `selfdev_repair_completed` / `selfdev_repair_failed`。
- 投递与匹配：`task_runtime/goal_events.py::publish_goal_event` 只写最小标识与元数据
  （`OutboxJob(goal_event_dispatch, operation_id=goal_event:<source>:<event_id>)`，载荷 2000 字符封顶），
  无活跃目标订阅时直接丢弃；`worker/handlers_goal_events.py` → `GoalTriggerService.dispatch_event` 按
  **事件类型 + 目标关联（线程级 / 全局）+ 规则条件**确定性匹配，仅 `event_condition`/`window_once`/
  `opportunistic` 参与，逐目标提交；无匹配返回 `goal_event_matched:0`，零 LLM 调用。
- 决策：匹配后仍用既有 `goal_trigger_decision` 作业（稳定幂等键 `event_condition:<event_id>`），
  决策作业额外携带 `trigger_event_type`/`trigger_event_payload`，消费者无需回查不存在的聊天事件行；
  服务端校验（依赖/审批/预算/每日上限）与主动消息写入路径完全复用。
- 窗口过期：`expiry_of` 现在返回 `window_once_expired:<start>` 稳定键，`_scan_task` 在置 MISSED +
  `expired_at` 后**再触发一次决策**；重复扫描与进程重启都不会二次唤醒。
- 可靠性：事件消费失败 → `RETRYABLE_ERROR` 交 Outbox 重试；重复事件由 `operation_id` 唯一键去重，
  同一事件对同一规则由 `step.last_trigger_key` 去重；用户暂停/结束目标后不再被唤醒（计划非 ACTIVE 直接跳过）。
- 测试：新增 `tests/unit/backend/test_goal_event_sources.py` 13 例（Fake LLM，全离线）；`test_goal_triggers.py`
  的窗口过期用例按新契约更新为“过期触发一次决策、重复扫描不重复”。
- 全量回归：1383 passed / 23 skipped / 0 failed（PG 与本地 Embedding 专项跳过）；basedpyright 0 errors。
- 遗留风险：任务事件只在**同一线程**内唤醒目标（跨线程不做广播）；知识维护/自进化事件是全局事件，
  所有订阅该类型的活跃目标都会各决策一次（受每日上限约束）；Outbox 死信后的事件不重放，需要人工重跑维护任务。

### V54 安全边界上的主动事件注入与协作式处理（2026-10-28）
- 目标：计划事件在 Agent 执行任务期间到达时，不并发启动第二个 LLM Run、不强制中断工具；事件先持久化，等当前工具调用批次完成、下一轮 LLM 决策前以内部上下文注入，由同一个 LLM 决定立即处理 / 延后 / 继续 / 无需行动，并可主动向用户发送真实助手消息。
- 数据与去重：新增 turn_event_inbox（db/models.py::TurnEventInbox + 迁移 f7a1c3d5e9b2），dedupe_key 唯一；GoalTriggerService._fire 在存在活跃前台 Turn 时入箱（priority：window_once 90 / event_condition 80 / opportunistic 60 / 其他 50），Outbox 仍是唯一可靠投递与重试通道；线程级积压上限 50，单次注入上限 5，超出部分留待下一次 assistant 调用，优先淘汰最老 pending。
- 注入边界：agent_graph._assistant 在每次 LLM 调用前检查 _tool_calls_paired；只有 assistant tool-call 与对应 ToolMessage 全部配对后才 claim 事件，把 build_internal_context 生成的 SystemMessage 追加到本次请求（不新增 LLM 请求、不写入聊天时间线），并从回复剥离 [[EVENT_DECISION: ...]] 标记；流式路径由 _DecisionMarkerStreamFilter 在 token 级剥离。内部上下文包含事件类型/来源/优先级/关联目标与步骤/触发原因/事件元数据/目标状态/TaskEvidence，并要求模型显式给出四选一决策。
- 主动消息：runtime/proactive_tool.py::notify_user 只注册到单 Turn registry 快照，服务端按 turn_record_id 校验“本 Turn 已领取事件”后才允许发送；ProactiveMessageService.post_in_turn 把消息写成同一 Turn 的 Event(llm_response, payload.proactive=True)，Event.id 由 uuid5(dedupe_key) 稳定生成，重复调用只落一次；通过既有 ws_manager 推送 new_message，工具返回后 Agent 继续原工具循环，最终答复由 _finalize_turn 以更大的 turn_event_index 追加，因此主动消息不会被误认为最终答案。
- 可靠性：claim 置 injected；Turn 正常收尾 mark_turn_handled（defer 保留给 Outbox，其余 handled）；notify_user 成功即标记 handled + message_event_id；Turn 中断/进程重启后 Outbox 看到 pending/injected/deferred 且无活跃 Turn → expire 并走原 GoalDecisionEngine；决策作业看到 handled → 直接 COMPLETED，绝不二次调用模型或重复发消息；前台 Turn 活跃时决策作业返回 RETRY_LATER，避免并发 LLM。取消/修改/审批仍走既有工具与安全内核。
- Trace 与可见性：turn_event_injection Event 保留完整内部上下文、决策与工具名；ThreadState 只渲染 user_message/tool_call/tool_result/llm_response，内部 SystemMessage 与 trace Event 不进入普通聊天。
- 测试：tests/unit/backend/test_turn_event_injection.py 9 例（Fake LLM，全离线）：工具执行期间事件到达、连续工具调用、多事件合并/优先级/注入上限、流式标记过滤、主动消息插入/顺序/幂等、原任务恢复、重复事件、Outbox 让出与重启恢复、handled 事件不二次决策、内部提示不泄露而 Trace 完整。
- 遗留风险：事件注入只在 assistant 节点入口发生，若模型单轮不再调用工具（直接结束），事件由 Outbox 在 Turn 结束后兜底决策，不会在本次 Turn 内提醒；notify_user 的授权依赖 turn_record_id，跨 Turn 的事件只能走后台主动消息路径；流式标记过滤只处理回复开头（V55 起改为全流式增量扫描，正文中间与跨分片标记同样不会进入实时 token）。

### V55 主动事件决策标记的全流式防泄漏（2026-10-28）
- 目标：V54 的流式过滤只在回复开头生效，标记出现在正文中间或跨多个 token 分片时会短暂泄漏到实时 token（最终持久化文本已被剥离，形成"流里看得到、库里没有"的不一致）。本轮改为全流式增量扫描，使 SSE token、最终助手消息与 __graph_result__ 回复逐字一致。
- 协议唯一事实源（runtime/turn_events.py）：DECISION_MARKER_WS_MAX=8、DECISION_DECISIONS、DECISION_MARKER_MAX_LEN；DECISION_MARKER_RE 的空白段改为限长（\s{0,8}），使合法标记长度有上界。新增 remove_decision_markers（最终文本清洗唯一入口，parse_decision_marker 复用）、scan_decision_marker（前缀状态机，返回 match/partial/none）、DecisionMarkerStreamFilter（feed/flush，有界缓冲）。
- 扫描规则：安全文本立即透传，缓冲尾部最多滞留一个 "[" 以等待跨分片的 "[["；命中完整标记整段剥离；partial 仅在长度 ≤ DECISION_MARKER_MAX_LEN 时等待，超过即判定为普通文本并放行（不会因为半个标记无限吞字）；非法片段与流结束时仍未闭合的片段原样输出，与正则清洗结果一致。
- 一致性：只有标记先于首个非空白字符时才丢弃前导空白（与 remove_decision_markers 的 lstrip 规则同构），因此流式累计文本与最终持久化助手消息逐字相同；过滤器仍只在本次 chat model 输入含 INTERNAL_CONTEXT_HEADER 时启用，普通聊天文本不受影响。
- 不改变 V54 语义：不新增 LLM 调用、不改事件决策判定（notify_user > 其他工具 > 显式标记 > continue）、Trace 仍保留完整内部上下文与决策，未删除任何审计记录。
- 测试：tests/unit/backend/test_decision_marker_stream.py 24 例（全离线）：标记在开头/中间/结尾、任意二分切点与逐字符分片、多标记、普通文本、非法与未完成标记、前导空白语义、缓冲有界、scan 三态，以及驱动真实 _execute_graph_stream 事件循环的 SSE 断言（token 无泄漏且等于最终回复；无注入时不过滤）。
- 版本索引修正：本文件此前只有 V34 的 Phase 标题，最新阶段靠文末追加，索引与主干不一致；现已在文件头补"版本索引"指向主干最新阶段。
- 遗留风险：超过 8 个连续空白的标记（如 "[[        EVENT_DECISION: continue]]"）不再被识别，会按普通文本展示且不剥离——这是"宁可展示也不无限缓冲"的确定性取舍；如需放宽，同步调整 DECISION_MARKER_WS_MAX（正则与扫描器共用该常量）。

### V56 事件收件箱溢出与决策串行化（2026-10-28）
- 问题：V54 的决策作业守卫只在 pending/injected/deferred 或“行不存在”时检查前台 Turn；一旦收件箱溢出把老事件置为 expired，对应作业就跳过前台检查直接走 GoalDecisionEngine——前台 Turn 仍在跑，却并发发起了第二次 LLM 决策。
- 守卫重写（worker/handlers_goal_triggers.py）：对携带 pending_event_dedupe_key 的作业，**只有 handled 直接 COMPLETED**；其余状态（pending/injected/deferred/expired/行被淘汰/行不存在）一律先查 active_turn_for_thread，Turn 持有有效租约就 RETRY_LATER，绝不调用模型；无活跃 Turn 时 open 状态先 expire 再走原 GoalDecisionEngine 兜底一次。
- 重试节流：等待前台 Turn 的 retry_available_at 对齐租约到期时间（+1s），并以 FOREGROUND_WAIT_MAX_SECONDS=60 封顶；task_busy 仍用 15s。Turn 正常收尾或崩溃失租后，下一次轮询即可接手，不会无限空转。
- 溢出与拒收（runtime/turn_events.py）：保留 50 条上限与“淘汰最老 pending”策略，排序改为 (created_at, id) 确定性；淘汰行置 expired 并写 decision=evicted_overflow，同时打带 key/limit 的 warning；无可淘汰 pending 时拒绝新事件，仅记 warning——决策作业本身仍在 Outbox，Turn 结束后按原路径兜底，不静默丢失。
- 不变项：复用既有 Outbox、幂等 operation_id 与状态机，不新增事件总线/任务引擎；V55 的流式标记过滤与事件决策语义未改动。
- 测试：tests/unit/backend/test_turn_event_overflow.py 8 例（Fake LLM，全离线）：溢出淘汰与原因、无可淘汰拒收、被淘汰事件在 Turn 活跃期间零 LLM 调用、重试点对齐租约并封顶、Turn 结束后只决策一次、被拒事件恢复、handled 不重复决策、缺失行同样让出、租约过期但状态仍 running（进程重启）能接手且只决策一次。
- 遗留风险：Outbox 为 at-least-once——若作业在 apply 提交后、finalize 前崩溃，重投可能对 message 类决策二次发言（act_now 类由 last_trigger_key 拦截）；彻底消除需要把“已决策”标记与 apply 写在同一事务（会改动 V54 既有断言，本轮未做）。

### V57 前后台 LLM 串行化与目标决策幂等（2026-10-28）
- 问题（两处真实并发/幂等缺口）：
  1. 决策作业是“查有没有活跃 Turn → 调模型”的**读检查**，检查通过到真正调用 LLM 之间前台 Turn 可能刚好启动，
     同一线程出现两个 LLM 调用；V54/V56 的 `active_turn_for_thread` 只是快照，无法关闭这个窗口。
  2. Outbox 是 at-least-once：作业在 apply 提交后、finalize 前崩溃时重投会**再调一次模型**，message 类决策会二次发言，
     `modify_plan` 会把 revision 推两次；V56 遗留风险里已登记此项。
- 线程级互斥（新 `runtime/thread_lease.py` + `db/models.py::ThreadExecutionLease` + 迁移 `a9d3e5f7b1c4`）：
  一行 `thread_execution_leases`（thread_id 主键、owner_kind、owner_id、fencing_token、lease_expires_at）表达
  “谁有权在该线程调用 LLM 并写时间线”。抢占是**条件更新**（租约到期或同 owner 重入才成功），每次抢占/续租/释放都是
  独立短事务，**不跨模型调用持有数据库事务**；`fencing_token` 单调递增，旧持有者续租/释放会被拒绝；进程崩溃后租约
  自然过期即可被接管（无人工清理）。
- 前台 Turn（`turn_execution.py`）：`_resolve_and_preempt` 在把 TurnRecord 置 running 之前抢占占用；后台决策持有
  时按 `ACQUIRE_POLL_SECONDS` 轮询等待（`FOREGROUND_WAIT_SECONDS=35s`，覆盖单次模型 30s 超时），超时返回既有
  `turn_in_progress`（HTTP 202 可重试）。**用户输入不丢失**：TurnRecord(not_started) 与 user_message 事件在更早的
  短事务里已经提交，同一 turn_id 重试走 `attempt_no += 1` 续跑，不会产生第二条用户消息。
  `TurnHeartbeat` 与 TurnRecord 租约一起续租线程占用（续租失败即 `lease_lost`，避免长 Turn 占用过期被后台抢走）；
  `_finalize_turn` / `_mark_interrupted` / `_persist_interrupted_tool_facts` 在各自事务内释放占用。
- 后台决策（`worker/handlers_goal_triggers.py`）：守卫（V56）通过后**先抢占占用**才允许调用模型，失败
  `RETRY_LATER(thread_lease_busy)`；占用一直持有到 apply、主动消息落库、Trace 写入提交完成。拿到占用后再用
  `active_turn_for_thread` 兜底，识别“升级前启动、没有占用行”的旧 Turn（让路，不并发）。
- 决策持久化与幂等（`task_runtime/goal_decision.py`）：稳定决策标识
  `decision_id_for(task_id, step_id, trigger_key) = uuid5`。模型返回后先 `persist_decision` 写
  `AgentTaskEvent(goal_trigger_decision_recorded, dedupe_key=goal_decision:<id>)`（复用既有表，不加新结构）；
  `load_persisted_decision` 读回结构化决策与“是否已应用”。重投时：已应用 → `COMPLETED(decision_already_applied)`
  只收尾；已决策未应用 → 直接复用决策，**不再调用模型**。
- 事务一致性：计划变更、`goal_decision` 记录、主动消息、`goal_trigger_decision_applied` 标记、Trace 在同一个短事务提交
  （`apply` 内部沿用 `ProactiveMessageService.post` 的 `ProactiveBlocked` 回滚语义）；apply 提交后崩溃，重投只能看到
  已应用标记，不会重复副作用。
- 主动消息唯一性：`ProactiveMessageService.post(dedupe_key=...)` 的 turn_id 与 `Event.id` 都由
  `uuid5(thread_id + dedupe_key)` 派生（`dedupe_key = goal_decision:<decision_id>`），不依赖生成文案；同一决策重放
  先按 `Event.id` 幂等返回 `created=False`。
- 不变项：V55 流式标记过滤、V56 收件箱溢出与事件恢复语义、V54 注入边界与 notify_user 授权均未改动；仍只有一次触发一次模型
  调用，不新增 LLM 请求；Outbox/幂等键/状态机全部复用。
- 测试：`tests/unit/backend/test_goal_decision_serialization.py` 8 例（Fake LLM，全离线）：占用互斥 + fencing + 按 owner
  释放、崩溃后租约过期被接管且旧 token 续租失败、后台让路前台占用（零 LLM 调用）、前台等待时用户输入已落库且重试续跑同一
  Turn、决策持久化后崩溃重投复用决策（模型只调用一次）、应用后重复投递只收尾（revision 只涨一次）、主动消息 ID 由决策标识
  派生且换文案不产生第二条、前台 Turn 正常收尾释放占用。
- 全量回归：1433 passed / 23 skipped / 0 failed（1425 基线 + 8 新增；跳过项为 PG 与本地 Embedding 专项）；相关套件
  （goal_triggers / goal_event_sources / turn_event_injection / turn_event_overflow / decision_marker_stream / chat_api /
  flow turn_execution / outbox_worker）102 passed。ruff --select F,E9 全通过；basedpyright 改动源文件 0 errors / 0 warnings。
- 遗留风险：① 前台最多等待 35s，超时后前端需要重试（消息已落库，不会丢；但用户会看到一次“稍后再试”）；
  ② 租约表按线程串行化，同一线程上的多个目标决策会排队（设计如此，代价是低频触发场景下的额外延迟）；
  ③ SQLite 下 `with_for_update` 不生效，跨进程抢占依赖单写者 + 条件更新；Postgres 路径由行锁 + 条件更新共同保证。

### V58 主动提问与用户回复关联闭环（2026-10-28）
- 问题：V50 的 ask_user / wait_user 只把步骤置 waiting_user 并写一条主动消息，**问题本身没有生命周期**——
  用户随后的自然语言回复只是一条普通聊天消息，系统无法把它关联回“哪个目标、哪一步、哪个问题”，
  闭环停在“提问 → 等待”，回答只能靠用户去任务中心点按钮。
- 持久化待回答问题（新 `task_runtime/goal_questions.py`，复用 `agent_task_events`，不加表）：
  `GoalDecisionEngine.apply` 在 ask_user / wait_user 时登记 `goal_question_asked`
  （dedupe_key `goal_question:<id>`，payload 含 task/step/decision/trigger/message_event_id/expires_at）；
  question_id = `uuid5(task_id + step_id + trigger_key)`，与 V57 决策标识同源，Outbox 重投不会开第二个问题。
  生命周期用事件表达：`goal_question_resolved`（一次解析）/ `goal_question_expired`（审计标记）；
  状态（open/resolved/expired/stale）在读取时推导，有效期 72h，**不新增调度器**，过期后一律不改目标。
- 关联用户回复（不加一次 LLM 调用）：`ContextAssembler.assemble` 在该线程有未关闭问题时把
  `render_questions_block`（≤1200 字符，含 question_id 与处理规则）作为 system 消息紧邻当前消息注入，
  并把列表放进 `agent_ctx["open_questions"]`；`agent_graph` 据此**只在该轮**把 `answer_goal_question`
  注册到 registry 快照（与 notify_user 同一模式，不改 `build_main_agent_registry` 固定工具面）。
  判定由本轮对话模型在同一次调用里完成：回答 / 修改计划 / 拒绝 / 普通聊天。
- 执行与校验（新 `runtime/question_tool.py`）：工具只接受服务端已登记的问题——
  多候选且未给 question_id → `clarification_required`（**绝不猜测绑定**）；已关闭 → `already_answered` /
  `question_expired`；审批步骤 → `step_requires_approval`（问题保持打开，仍走原审批机制）；
  成功则 `GoalService.answer_step_question`：answer 复用 `send_input` 记录回答 + `waiting_user→ready` + 入队，
  modify_plan 复用 `modify`，reject 标记 skipped 并写 `goal_step_answered`。
- 幂等与恢复：解析事件 `goal_question_resolved` 带 user_event_id / answer / intent / outcome，
  同一问题只写一次；重复消息（同 user_event_id）或 Outbox 重投只回报已处理，不重复改计划；
  问题与回答都在数据库里，进程重启后仍可关联；回答内容进入 `task_state.inputs`，下一次 Run 能读到。
- 不变项：V50 提醒与沉默策略、V54 事件注入、V55 流式过滤、V56 收件箱、V57 线程级占用与决策幂等全部未改动；
  不新建消息系统、不新增 Agent、不为每条用户消息额外调用模型。
- 测试：`tests/unit/backend/test_goal_question_answer.py` 11 例（Fake 会话，全离线）：单问题回答解除等待并记录
  user_event_id、多候选必须澄清且显式绑定只影响对应步骤、普通聊天不误关联（无问题 / 模型不调用）、
  modify_plan 复用 GoalService、reject 记录拒绝并 skipped、审批步骤拒绝、过期问题只标记不改目标、
  同 user_event_id 重投只应用一次、决策重放不产生第二个问题、重启后仍可关联且不可重复应用、上下文只注入 open 问题。
- 全量回归：见交付报告 `docs/reports/2026-10-28_V58_question_answer_loop_report.md`；ruff --select F,E9 全通过；
  basedpyright 改动源文件 0 errors / 0 warnings。
- 遗留风险：① 问题与回答的关联依赖模型在本轮主动调用工具，模型不调用时问题保持打开（用户可再发一条或去任务中心）；
  ② 同一线程同时存在多个问题且用户表述含糊时，需要用户再说一句确认（这是刻意的，不做猜测绑定）；
  ③ 问题生命周期仍存放在 `agent_task_events`，按线程查询依赖 (task_id, created_at) 索引，规模随线程任务数增长。

### V59 待回答问题的状态一致性（2026-11-02）
- 问题：V58 的待回答问题只判断「是否已回答 / 是否过期 / 任务是否终态」。目标暂停、结束，或步骤被修改、重置后，
  旧问题仍满足这三个条件，于是继续被注入聊天上下文并绑定回答工具；用户回复后才在服务端被拒——过期业务状态污染正常对话。
- 修复（只改读取侧的派生判定，不加表 / 不加调度器 / 不加 LLM 调用）：
  - `goal_questions._plan_snapshot` 从既有 `task_state["goal"]` 读目标状态与全部步骤状态；
  - `_invalid_reason` 依次校验：任务终态 → 计划缺失 → 计划非 active（paused/blocked/completed/rejected/abandoned）
    → 步骤不存在 → 步骤不是 waiting_user → 被同步骤新提问取代；
  - `_superseded_ids` 让同一 (task, step) 只保留最后一次提问（步骤重置后重新提问不会让旧问题继续挂着）；
  - 失效仍是读取时推导的 `stale`，原因写进 `PendingQuestion.invalid_reason`（goal_paused / step_not_waiting_user / superseded 等）。
- 注入面：`ContextAssembler.assemble` 与 `agent_graph` 都以 `list_open_questions` 结果为准，因此目标暂停后普通聊天
  既不注入问题块、也不绑定 `answer_goal_question`；恢复目标且步骤仍在 waiting_user 时问题自动重新有效（不写任何标记事件）。
- 服务端校验：`GoalService.answer_step_question` 在写计划前复核 question_id（存在则要求 task/step 匹配且问题仍 open），
  旧 id 直接调用抛 `question_stale:<reason>`；`runtime/question_tool.py` 把已关闭/失效问题统一映射为
  `already_answered` / `question_expired` / `question_stale` + reason，一律不改目标；审批步骤仍先走 `step_requires_approval`，
  审批边界没有被本次修复绕过。
- 测试：`tests/unit/backend/test_goal_question_state_consistency.py` 10 例（全离线）：暂停后不注入且回答只回报 question_stale、
  直接服务端调用被拒、步骤改内容只让该步骤问题失效（同线程其它问题仍可答）、同步骤新提问取代旧问题、
  恢复后有效问题可答、恢复后已变化步骤的问题仍失效、审批步骤恢复后仍需审批、失效/过期/重复回复都不改目标。
- hint 按失效原因分支（`_stale_hint`）：暂停说明「恢复后原问题重新有效、不需要重新发起」，步骤变化说明「等新提问」，被取代说明「回答最新的那个」——避免暂停场景误导用户重建目标。
- 全量回归：1453 passed / 23 skipped（PG 与 TEI 专项按环境跳过）/ 0 failed；ruff `--select F,E9` 全通过；
  basedpyright backend 0 errors / 0 warnings。
- 遗留风险：① 判定依据是 `task_state["goal"]` 的步骤状态，绕过 GoalService 直接改写计划会让判定跟着脏数据走；
  ② 同一 (task, step) 的旧问题会被新提问取代，模型拿旧 question_id 回答只会得到 question_stale（刻意 fail-closed）；
  ③ 暂停期间不写失效事件，审计依据是 `goal_paused`/`goal_resumed` 与问题状态推导结果。

### V60 记忆检索架构升级：pg_search BM25 + 统一排序（2026-11-03）
- 问题：原 Lexical 路由是 ILIKE + CJK bigram 词重叠，不是 BM25（无词频饱和/IDF），
  且每条候选都要把正文拉回 Python 打分；Recent Episode 是独立路由，按时间倒序，
  容易出现「最近事件无条件压过长期偏好」。
- 索引：ParadeDB pg_search 0.26.4（PostgreSQL 16.15 + pgvector 0.8.7），BM25 索引直接
  建在权威表 memory_records：content / keywords 用 pdb.jieba，canonical_key 用
  pdb.literal_normalized（保留标识符整串匹配）。与表同事务维护，无第二份事实源。
- 召回：AutomaticRecallEngine = Exact + Dense(pgvector) + Sparse(pg_search BM25)；
  _route_recent_episode 删除，情节记忆改由两路召回、时间特征进统一排序；
  _route_lexical 仅作 BM25 不可用时的降级路径。
- 排序：recall_fusion 用 RRF（k=60）融合排名，不直接相加 cosine 与 BM25 原始分；
  最终分 = 0.40*rrf + 0.20*relevance + 0.20*recency + 0.10*importance
  + 0.05*trust + 0.05*scope；recency 用类型半衰期（episodic 3d / knowledge 180d /
  profile 365d）乘查询意图系数（情节记忆在非时间敏感查询下折减 0.35）。Exact 候选
  跳过相关性阈值，只受 token 预算约束。
- 安全：所有路由候选在融合前统一经 ForgetVisibilityService 批量过滤（fail-closed），
  BM25 只返回 ID/分数并回源复核 lifecycle/validity/scope，遗忘后 content scrub、
  物理删除、事务回滚都由 PG 保证索引一致。
- 降级：扩展/索引不可用时 route=lexical，MemoryRecallPack.degraded=true +
  degraded_reason，并经 UnifiedRetriever notes 传到 RetrievalResult.degraded 与
  检索诊断，不谎称 BM25 生效。
- 部署：deploy/pg/Dockerfile（pgvector + pg_search）、compose postgres 改 build、
  AIIVE_MEMORY_BM25_ENABLED / AIIVE_MEMORY_BM25_TOP_K；迁移
  c3f7b2a9d1e4_add_memory_bm25_index 幂等，扩展缺失时 WARNING 跳过不阻断升级。
- 验证：全量 1472 passed / 44 skipped / 0 failed；真实 PG+pg_search 21 passed
  （中英混排、标识符、双路去重与 RRF、Recency、Scope、forgotten/archived、
  Shield/Tombstone、UPDATE/DELETE/ROLLBACK、降级、AUTO 全链路）；PG 契约 10 passed；
  alembic check 无漂移；改动文件 ruff 全通过、新模块 basedpyright 0 errors。
- 交付报告：docs/reports/2026-11-03_V60_bm25_recall_report.md；
  实现细节：docs/architecture/memory_retrieval_bm25.md。
- 遗留风险：① 本机 .env 未改，需手动开启 BM25 并重建 postgres 镜像；
  ② 索引为普通 CREATE INDEX，大表需停机窗口；③ 单字中文查询不命中；
  ④ pg_search 版本与 PG 主版本强绑定。

### Forget 来源 Event 隔离与摘要重建隐私闭环（2026-11-04）
- 目标：修复 Forget 对 Memory 来源 Event 的隔离缺陷，确保被遗忘记忆不能经原始历史、记忆重新提取、SegmentSummary 重建重新进入正常上下文；严格遵循 phase_6A 的 memory_only / history_only / everywhere 语义，不重写整个 Saga。
- 来源持久化：`MemoryWriteService._write_evidence_batch()` 同事务写 `ContentProvenanceRef(owner_type=memory_record, source_type=event)`（去重幂等，Evidence 删除后仍存活）；Alembic 数据迁移 `f1a2c3d4e5b6` 从 `memory_evidence.source_event_id` 回填历史数据（幂等、PostgreSQL/SQLite 兼容、无 DDL）。
- 可见性：`ForgetVisibilityService` 对 Event 追加「所属 TurnRecord 被遗忘」「作为被遗忘 MemoryRecord 来源」屏蔽，`is_tombstone_blocked_reingestion()` 沿 Provenance 拦截再提取，`can_audit_read()` 多 Tombstone 冲突 fail-closed；保留 cutoff 与 fail-closed，不阻断无关新 Event、无递归死循环。
- 屏蔽时序：有界显式 memory_ids 在 Phase A 事务立即写来源 Event 实体 Shield；宽选择器/超限 ID 不在 Phase A 无界枚举；Cascade 持久化来源 Event Tombstone（allow_audit_read 随 Mode），Shield 退休后仍长期屏蔽。
- 摘要重建：`_try_generate_summary()` 用统一可见性规则在 50 条上限前过滤全部有效 Event/Turn Tombstone 与来源已遗忘 Event，LLM 只收允许来源，新 Summary 的 source_event_ids / source_turn_ids / source_hash 仅取允许来源；`_rebuild_summaries_batch()` 真实调用链有当前及后续 Operation 的回归测试。
- mode 语义：`_scrub_batch()` / `_scrub_wide()` 对 memory_only 保留来源 Event 原文（含独占来源），history_only / everywhere 维持不可逆清理；正常可见性屏蔽与防重抽保留。
- Verify：`_verify_source_event_integrity()` 校验来源 Event 屏蔽覆盖、新 Summary 不引用被屏蔽 Event、memory_only 未误清、everywhere/history_only 清理完成；无法证明完整性不落严格 verified。
- 迁移链：HEAD 由 `b8d2e6f4a1c7` 推进到 `f1a2c3d4e5b6`（纯数据迁移，无 schema 变更）；CODE_MAP 已同步。
- 验证：Forget 集成测试 65 passed；全量 pytest、Ruff、basedpyright、Alembic upgrade/check 见最终报告；PostgreSQL 专项按环境记录。
- 遗留风险：宽范围 Scrub 的 JSON 数组匹配、scope/canonical_key 分支、mode 与 selector 契约、多用户边界、Task Runtime / Knowledge 对象与对象存储覆盖、Checkpoint/WorkingState/LLMCall preview 严格内容证明仍为既有审计缺口，不在本次范围。

### Forget 防重抽闭环 / Mode-Selector 契约 / Turn ID 一致性（2026-11-05）
- 目标：修复 Phase A→Cascade 之间的防重抽漏洞、混合 Evidence 绕过、Mode/Selector 组合不一致、TurnRecord ID 清理不一致；不重写 Saga、不新增模型、不引入新 LLM。
- 防重抽：`is_tombstone_blocked_reingestion()` 同时检查 active Shield（Event/Memory/Turn 实体 + thread/time_range/scope/canonical_key/all_user_data，应用 cutoff）与 `Tombstone.block_reingestion`；`blocked_target_ids(include_tombstones=False)` 将 Shield 与可见性 Tombstone 解耦，Phase A 后、Cascade 前旧来源即不可重抽，cutoff 后新 Event 不受阻。
- 混合 Evidence：`write()` / `_write_single_in_transaction()` 改为任一来源被屏蔽即拒绝整个 Proposal；被拒绝 Evidence 不写 `MemoryEvidence` / `ContentProvenanceRef`，Reinforce/Merge/Supersede 无法绕过。
- Mode/Selector：新增 `validate_selector_combination()`，API/Tool/Phase A 共用；只允许一种选择器；memory_only 仅 memory_ids/canonical_key/scope，history_only 仅 thread/turn_ids/event_ids/time_range；非法组合在创建 Operation 前拒绝。
- Turn ID：`_normalize_turn_tokens()` 统一 TurnRecord.id/turn_id；依赖发现、子 Event Tombstone、`_scrub_event_turn_targets()` 全部走该入口；`content_purged` 仅在确认无剩余正文后置 True。
- Verify：要求来源 Event 具备不依赖 active Shield 的持久化 Tombstone（block_visibility+block_reingestion）；event/turn 目标双标志校验；Turn 子 Event 长期 Tombstone 校验；仅活动 Shield 不得落严格 verified。
- 验证：Forget 集成测试 95 passed；全量 pytest、Ruff（--select F,E9）、basedpyright 0/0、SQLite 专项、PostgreSQL 按环境记录；文档已同步。
- 遗留风险：宽选择器 `_scrub_wide` 的 JSON 数组匹配与 scope/canonical_key 分支、Checkpoint/WorkingState/LLMCall preview 严格内容证明、Task/Knowledge 对象与对象存储覆盖仍为既有审计缺口。

### Forget × MemoryWrite 并发安全与拒绝路径隐私闭环（2026-11-05）
- 目标：修复两个 P0 —— ① `Gate Reject` 分支在防重抽检查之前持久化 Proposal 原文；
  ② Phase A Shield 与 MemoryWrite 之间缺少共同并发控制（检查通过后、Shield 提交前后交错写入）。
  不重写 Saga、不新增数据模型、不改 Schema、不引入 LLM 调用。
- 拒绝路径隐私：`write()` / `_write_single_in_transaction()` 的检查顺序改为
  `_is_reingestion_blocked()` → Gate → Resolve → Execute → Persist；命中即整条 Proposal 拒绝，
  **不写** `memory_records` / `memory_evidence` / `content_provenance_refs` / `memory_proposals`，
  也不保留最小审计（避免旁路重新持久化）。普通 Gate Reject 仍写原有审计行。
- 检查范围统一：`evidence[*].source_event_id`、`source_event_ids`、`source_turn_record_id`、
  `source_turn_id`、`RunContext.turn_record_id` / `turn_id` / `source_event_ids`；`write_batch`
  内按来源 id 做单事务 memo，避免同批次重复查询。
- 并发控制：写入路径取 `pg_advisory_xact_lock_shared(0x41495645, 1)`，`forget()` 取对应
  `pg_advisory_xact_lock` 排他锁；双 int4 命名空间与 canonical_key 的 bigint 锁（objsubid 1 vs 2）
  互不干扰；锁序「全局 → Memory Key」；锁由事务持有至 COMMIT/ROLLBACK；
  `concurrency.ensure_read_committed()` 对非 READ COMMITTED fail-closed。
- 情况 A：Forget 等待在途写入提交后才获锁，`cutoff` 在获锁后计算，等待期间提交的记忆被本次
  Forget 的选择器 Shield 覆盖；情况 B：写入等共享锁，Forget 提交后读到 active Shield 并拒绝。
- 纯 Turn 来源：`is_tombstone_blocked_reingestion(source_turn_record_id=...)` 统一展开
  `TurnRecord.id` / `turn_id`，实体 Shield 双写法匹配，选择器 Shield 经
  `blocked_target_ids("turn_record")` 以 `TurnRecord.created_at` 应用 cutoff（Forget 之后的新 Turn 不阻断），
  Tombstone 同时匹配 `source_turn_record_id`、`target_type=turn_record + target_id` 与子 Event。
- 测试：`tests/integration/backend/test_forget_write_privacy.py` 15 passed；
  `tests/integration/pg/test_pg_forget_write_concurrency.py` 6 用例（两个独立 PG 连接 + 真实事务，
  断言 `pg_locks` 中 ShareLock/ExclusiveLock 的授予状态），位于既有 `postgres-contract` Job 的采集目录。
- 验证：全量 pytest 1637 passed / 59 skipped；Forget 集成 + 隐私用例 + 写入单测 123 passed；
  ruff `--select F,E9` 全仓通过；basedpyright 改动文件 0 errors / 0 warnings。
- 遗留风险：① 非 READ COMMITTED 部署会直接报错（刻意 fail-closed，需要改回默认隔离级别）；
  ② 同一事务内「先 forget 再 write」依赖同后端锁不自我冲突（PostgreSQL 允许），已在冲突确认流程覆盖；
  ③ time_range 宽范围 Scrub 与 JSON 数组清理仍留待 V62.2。

### Forget Phase A 统一锁入口与 CI 失败状态修复（2026-11-05）
- 目标：修复 Phase A 排他锁只覆盖 `MemoryWriteService.forget()`、未覆盖真实 API / Tool 入口的缺口；
  同时修复 CI 单文件隔离测试失败被 `tee` 吞掉的问题。不重写 Saga、不改锁命名空间与
  memory_only / history_only / everywhere 语义。
- 锁模块：新增 `backend/aiive/forget/concurrency.py`，统一提供
  `acquire_forget_shared_lock()`、`acquire_forget_exclusive_lock()` 与
  `ensure_read_committed()`。锁仍为双 int4 命名空间 `(0x41495645, 1)`，事务级
  advisory lock，持有到 COMMIT/ROLLBACK；PostgreSQL 非 READ COMMITTED fail-closed，SQLite no-op。
- 统一排他入口：`execute_phase_a_shield()` 在计算 cutoff、创建 Operation / Shield /
  Tombstone **之前**取排他锁。真实入口链：REST `routes_forget.py::forget_endpoint` →
  `handle_forget()` → `execute_phase_a_shield()`；Agent Tool 经 `builtin_tools` 注册的同一
  `handle_forget()`；`MemoryWriteService.forget()`（含 `routes_memories.py` 与
  `MemoryConflictService` 的 `forget_old`）→ `execute_phase_a_shield()`。
  `forget()` 内重复的排他锁调用已删除。顺带修复 Tool 入口无 `requested_by` 时生成
  `forget:<uuid>`（43 字符）超过 `forget_operations.requested_by VARCHAR(36)`、在 PostgreSQL
  上必然失败的问题（改为直接使用 36 位 UUID；SQLite 不校验长度所以此前未暴露）。
- `forget_old` 同事务兼容：同一 Session 先 `forget()`（排他）再 `write()`（共享）；
  PostgreSQL advisory lock 会话内可重入，不自我阻塞、不构成锁升级、不破坏原子性，
  已用真实 PG 用例验证。
- 测试：`tests/integration/pg/test_pg_forget_write_concurrency.py` 扩至 11 用例，新增
  `handle_forget()` 等待在途写入、`handle_forget()` 排他锁阻塞写入并使其读 Shield 后拒绝、
  REST 路由同锁保护、直接调用 `execute_phase_a_shield()` 无法绕过（lock_timeout 失败且零写入）、
  `forget_old` 同事务先排他后共享；断言真实数据结果而不只看 `pg_locks`。
- CI：`pytest` 与 `single-file` 两个 `| tee` 步骤都加 `set -o pipefail`，脚本失败不再被
  `tee` 的 0 退出码覆盖；无其他 `| tee` 门禁步骤。
- Supervisor 隔离测试根因：① `test_recover_after_interrupted_switch_rolls_back`：
  `pid_alive()` 把已退出但未被父进程回收的僵尸进程（Linux `/proc/<pid>/stat` state=Z）
  误判为存活，跨 Supervisor 停止接管进程时白等 15s + 5s 后失败；修复为识别僵尸进程。
  ② `test_app_exit_during_monitor_rolls_back_to_previous`：SIGKILL 生效是异步的，
  `tick()` 可能在进程尚未被回收时先判定「存活」、随后健康探针失败，把退出误报为
  `monitor_health_failed`，且漏记 `process_exited`。修复三处：检测到退出后补一次
  `reap()` 并记录事件；健康探针失败后再次确认存活，已退出则转入退出路径归因；
  测试的 `_kill` 以有界轮询等待进程真正退出（同步竞态，而非放宽断言）。
  未删断言、未跳过、未延长产品超时。
- 验证：全量 pytest 1637 passed / 64 skipped（新增 5 个 PG 用例默认 skip）；
  Forget 集成 + 隐私 + 写入单测 123 passed；冲突确认 30 passed；Supervisor 单文件 19 passed；
  PostgreSQL 并发 11 passed（全目录 51 passed / 13 skipped，skip 均为本地 Embedding 不可用）；
  ruff `--select F,E9` 全仓通过；basedpyright 0 errors / 0 warnings。
- 遗留风险：① 单事务内若先取共享锁再取排他锁，两个并发事务可能形成锁升级死锁；当前
  生产路径只有 `forget_old` 的「先排他后共享」，无此顺序，新增调用方必须遵守
  「排他 → 共享」或将其拆分为两个事务；② 僵尸识别仅覆盖 Linux（`/proc`），macOS 上仍是
  保守的 `kill(pid, 0)` 语义。


### Forget 宽范围 Scrub、Task/Knowledge 覆盖与完整性证明（V62.2，2026-11-05）
- 目标：修复宽范围 Scrub 覆盖面不足、JSON 数组误用标量 IN、Task Runtime/Knowledge/对象存储
  未纳入、宽选择器 Verify 只做粗粒度声明的问题；不重写 Saga、不改并发锁协议与 Mode 语义。
- 矩阵：`docs/architecture/forget_content_matrix.md` 列出每张表/字段、来源关系、Selector、
  Mode 处理、Scrub 操作与 Verify 方法；明确 tasks≠agent_tasks、artifacts≠task_artifacts。
- 范围：新增 `forget/content_scope.py`，从真实 ORM 列/JSON 引用解析 thread / time_range /
  all_user_data / canonical_key / scope / turn_ids / event_ids / memory_ids 的清理范围；
  memory_only 保留 Event/Turn 正文，其余模式按矩阵清理；canonical_key/scope 只处理目标
  Memory 与可证明派生，不扩大到无关用户数据。
- JSON：`select_json_array_members` / `count_json_array_members` 在 PG 用
  `jsonb_array_elements_text`、SQLite 用 `json_each`，批量分片；Summary/Checkpoint 的
  source ids 不再使用标量 IN 或 LIKE。
- 清理：新增 `forget/content_scrub.py`，覆盖 Memory/Proposal 五字段/Evidence span/
  Maintenance/CoreBlock/Retrieval、Summary 10 字段、Checkpoint 8 字段、Compaction、
  WorkingState/Context/LLMCall/Task/Artifact/Attention/Inbox、Task Runtime 全部内容副本、
  Knowledge Document/Chunk/Vector。
- 对象：DB 事务内解除引用并登记 pending `delete_object` 动作，COMMIT 后才删除；
  共享对象复查后跳过；失败保留 failed 并让 Purge 可重试；Verify 检查动作与对象实际存在。
- Verify：逐表重新读库统计残留 + 未知 JSON 结构 + 对象动作；残留 → `failed_retryable`，
  Shield 保持 active；全部通过才进入 `verified_with_coarse_purge`（宽）或 `verified`（窄），
  报告写入 Verify 动作 details。
- 测试：新增 `tests/integration/backend/test_forget_content_coverage.py`（17 用例，
  SQLite 真实 _scrub_wide/Purge/Verify 链路、对象删除与重试、共享对象、Knowledge、
  memory_only/history_only/everywhere、canonical_key/scope、time_range）；
  `tests/integration/pg/test_pg_forget_json_membership.py`（3 用例，PG JSON 成员与宽清理）。
- 验证：全量 pytest 1654 passed / 67 skipped；`tests/integration/pg` 54 passed / 13 skipped
  （skip 为本地 Embedding 不可用）；ruff `--select F,E9` 通过；basedpyright 0/0。

### Forget 范围冻结、检索一致性与全内容承载完整性（V62.3，2026-11-05）
- 目标：修复 thread/all_user 未完整应用 Phase A cutoff、Retrieval Index 残留、Task Runtime
  time_range 父子遗漏、冲突/召回/Outbox 等辅助表未覆盖、Verify 字段缺口。
- Cutoff：直接记录统一按真实时间列 <= `manifest.cutoff_created_at`；派生记录按
  Provenance/JSON 成员清理（即使创建于 cutoff 之后）；cutoff 后独立新数据保留。
- 检索：Entry 发现纳入 segment/epoch/Summary/Checkpoint；Scrub 删除 Token、置
  is_searchable/is_current=false；`fetch_entries_by_ids` 过滤 is_searchable；真实
  UnifiedRetriever 验证 exact/lexical 前后召回差异。
- Task Runtime：time_range 按子表时间命中并反向找父 Task，只清命中子记录；父 task_state
  引用替换为 [forgotten]；无法恢复的运行中任务转 cancelled。
- 辅助表：MemoryConflictCase（pending/asking→stale）、MemoryRecallRun、RetrievalRun、
  TurnEventInbox（turn/event）、tasks.source_event_id、approval task/action、llm trace、
  outbox 正文键与 error_message。
- Verify：新增 SCRUBBED_FIELDS ⊆ VERIFIED_FIELDS 自动化约束；补齐 Agent/Task/Approval/
  Tool/Compaction 字段；未知 JSON 引用只在本次范围统计并 fail-closed。
- 测试：`test_forget_content_coverage.py` 25 用例（cutoff 新旧数据、派生清理、真实召回、
  Task 子记录、冲突/召回/检索/Outbox（含无 trace）、未知结构、字段子集、132 个 Scrub 字段
  逐项注入残留）；PG 10 用例（JSON 成员 + cutoff/派生清理 + Task 子记录 + 冲突/辅助表 +
  未知结构 + 真实 UnifiedRetriever 召回 + 跨表字段残留注入）。
- 验证：全量 pytest 1662 passed / 74 skipped；`tests/integration/pg` 61 passed / 13 skipped
  （skip 为本地 Embedding 不可用）；ruff `--select F,E9`、basedpyright 0/0 通过。

### Forget Outbox、Turn/Event 与 Task 时间范围一致性（V62.4，2026-11-05）
- 目标：修复 V62.3 审查发现的四类问题；不重写 Saga、不改锁协议/检索实现/Mode 语义。
- Outbox：job_type 字段契约 + 递归正文清理 + 引用 ID 保留；异步提取在 LLM 前与写入前
  双重 fail-closed；无关 Job 不取消。
- Turn/Event：cutoff 与 time_range 严格应用于子 Event；新 Event 保留，旧来源派生清理。
- Task：父子记录独立范围；父命中不扩大到全部子记录，子命中不整体清空父历史。
- 未知 JSON：候选范围先行 + 异常元素 fail-closed，发现盲区消除。
- 测试：SQLite 10 用例、PG 6 用例，覆盖真实 Outbox Handler、并发 LLM 竞态、
  三类 Task 时间组合、Turn/Event 边界、未知结构与残留注入。
- 验证：全量 pytest 1672 passed / 80 skipped；`tests/integration/pg` 67 passed / 13 skipped
  （skip 为本地 Embedding 不可用；Windows 文件权限竞态用例复跑通过）；ruff、basedpyright 0/0。

### Outbox 精确关联、执行授权与递归 Scrub（V62.5，2026-11-06）
- 目标：修复 V62.4 审查发现的三类问题（Outbox 误清理 cutoff 后新任务、Forget×LLM 检查后竞态、
  嵌套 List 清理不完整）；不重写 Saga、不改锁命名空间与 Mode/Selector 语义。
- Outbox 发现：`_discover_outbox_jobs` 按 `id` keyset 每批 500 行扫描，用
  `job_reference_ids()` 的结构化引用做精确集合交集；Event/Turn/Memory/子记录/trace 为强关联
  （不受 cutoff 约束），仅 Thread/父 Task 的弱关联必须同时满足 cutoff 与选择器范围；
  彻底移除 `operation_id.contains()` 与 `CAST(payload AS Text).like()`。
- operation_id：`operation_id_tokens()` 按冒号拆 token、剔除业务前缀与版本/批次 token 后整段精确相等；
  `<turn_id>EXTRA` 类前缀相同串不命中。
- 执行授权：新增 `worker/outbox_execution_auth.py`，独立连接 session-level `pg_advisory_lock_shared`
  与 Phase A 排他锁互斥，覆盖「重读 Job → 检查 Shield/Tombstone → LLM → 写入提交」；
  `refresh_claimed_job()` 重新校验 status/claim_token/locked_by/lease/最新 payload/redacted 标记。
- Handler：`handle_memory_extraction`（Job 行必须存在）、`handle_goal_event_dispatch`、
  `handle_goal_trigger_decision`、`handle_reminder_delivery` 全部接入，不再使用领取时的旧 payload。
- Scrub/Verify：`redact_job_payload()` 与 `payload_has_content()` 递归支持 dict/list/嵌套 list/scalar，
  顶层 List 同样清理；引用无法完整解析的 Job 写入 Verify `warnings`（显式记录、不阻塞终态）。
- 测试：SQLite `test_forget_v625_outbox.py` 7 用例；PostgreSQL `test_pg_forget_exec_auth.py` 4 用例
  （锁互斥 + Case A/B 双连接真实时序 + 精确关联/嵌套 List）。
- 验证：全量 pytest 1680 passed / 85 skipped；`tests/integration/pg` 72 passed / 13 skipped；
  ruff `--select F,E9` 通过；basedpyright 0 errors / 0 warnings；alembic `upgrade head` + `check`
  在 PostgreSQL 上「No new upgrade operations detected」。

### Job 来源守卫、执行授权全覆盖与精确关联补齐（V62.6，2026-11-06）
- 目标：Phase A 提交后、Purge 之前的旧 Job 不得再调用 LLM/Embedding/副作用；补齐授权覆盖与精确关联。
- 来源守卫：`worker/job_source_guard.py` 统一「严格 claim → 类型化来源 → Shield/Tombstone」，
  失败即 `forget_source_blocked`（fail-closed），覆盖 12 类 Job 的来源映射。
- 授权：13 个 Handler 接入 `@with_execution_authorization`；Phase A 与共享授权显式交接
  （执行 Forget 前释放、提交后 resume），三条嵌套路径不死锁。
- claim：`running` + 非空 token + 严格 owner + 未过期 lease，其余一律 CLAIM_LOST。
- 精确关联：`conflict_case_ids` / 新增 `tool_operation_ids` 纳入 Outbox 匹配；
  `retrieval_index_refresh` 按 `source_type` 解析；异常引用结构 fail-closed。
- 元数据：`COMMON_CONTENT_FIELDS` 移除 `trigger`，`state/plan/progress/details` 按 job_type 归类。
- 证据：`EvidenceCollector` 过滤被 Shield 覆盖的 Task/Action/Job 证据。
- 测试：SQLite 12 用例、PG 3 用例（真实 Phase A 拦截、嵌套 Forget 不死锁、Forget 等待在途执行）。


### V62.6.1 Forget 来源闭包与跨线程执行授权修复（2026-11-06）

- 基线：`f966dae48c22eceb7bf191be27bf57a212c9b112`；fetch 后 origin/main 与 HEAD 一致，开始时工作区干净。
- 状态：completed。代码、功能与资源清理验收完成；用户授权的两份历史测试库已清理，新测试库自动清理验证通过。全量 1716 passed / 118 skipped；真实 PostgreSQL 105 passed / 13 skipped；Ruff、basedpyright、前端构建、隔离 PG Alembic upgrade/check 均通过。完整证据见 `docs/audit/v6261_acceptance.md`。
- 实现：三个 P0 与四个 P1 的代码路径、来源缺失规则 1、真实 PostgreSQL 线程池调用链回归；无新 API、表或 Alembic 迁移。
- 可观察行为：Event Shield 立即阻止其 Memory 进入 Embedding/投影/维护；正常新数据和已证明的独立人工记忆仍可执行；旧 Task 无法用新 Job 时间绕过 Shield；被遗忘证据不进入 LLM；混合 WorkingState 按条目过滤。
- 限制：来源不明的历史 Memory/WorkingState 采用隔离/阻止新快照，未自动推测来源；Phase A 已开始时等待确定事务结果，可超过通用工具超时。真实外部 Embedding smoke 不属于离线验收。
- 清理：SQLite rollback / tmp_path；PG 测试使用一次性容器，fixture teardown 删除；Alembic 使用独立一次性容器并在 finally 清理，保留原有用户容器。

- V62.6.1 清理补验：根 conftest 先关闭 Session/连接池再删除本进程测试库，失败写报告并返回非零码；真实子进程回归 3 passed，最终全量 1716 passed、PG 105 passed，清理报告 verified。

### V63 单会话闭环修复与跨端状态一致性（2026-11-09）
- 基线：`750a06b`（唯一长期对话 / 互斥执行 / 远程中断首版）；本轮为闭环修复。
- 状态：completed。P0/P1 全部落地，单测 1226 passed；integration+flow 574 passed / 119 skipped；Web/Android build 通过；ruff F,E9 与 basedpyright 通过。
- 强制唯一 Thread：`TurnExecutionService._resolve_and_preempt` 对 `MessageSource.USER` 每次读取 `get_current_thread_id()`；缺省使用当前 Thread，显式与之不一致时在租约/TurnRecord/Event 之前抛 `stale_thread_id`，不再通过 `ensure_committed_thread` 合法化旧 Thread。`system_command` / `runtime_event` 保持原有线程行为。
- 启动握手：Web/Android 新增 `conversationReady`（初始 false），严格按 current Thread → 服务器历史 → execution status 顺序成功后置 true；Ready 前禁用输入与发送。Web 不再把 localStorage 的 threadId 当作启动权威（Android 同理），本地缓存只做展示。收到 `stale_thread_id` 时撤回本次乐观气泡、恢复原始输入（不覆盖用户已重新输入的内容）、重新握手。
- 流式 Busy 契约：`execute_turn_stream` 对冲突统一输出结构化 error（`turn_in_progress`：202 + retryable；`stale_thread_id` 等：409），经 `routes_chat._chat_error_payload` 转换；SSE 的 HTTP 状态保持 200，事件内 status 正确。Android 新增 `ChatApiError`（code/status/retryable）；两端在未收到 started 时收到 turn_in_progress 即删除 user/assistant 两个乐观气泡、恢复输入并重新读取 `GET /conversation/status`。
- 取消与记忆保护：`handle_memory_conflict_reply` 在解析前后各查一次 `status == cancelled || cancel_requested_at != null`；命中直接返回 COMPLETED / turn_cancelled，不调用 `apply_decision`，不改冲突案例、MemoryRecord、Evidence 或 Graph。真实并发测试用阻塞解析器在解析中取消 Turn 验证。
- 跨端状态：远端 Turn 是否活跃（`remoteTurnActive`）与 UI 提示 `remoteRunning` 解耦；running（含 `interrupting`）置位，只有 idle 清除；此前存在远端 Turn 时，idle 后立即以服务器历史覆盖本地消息（其他设备取消后本端用户消息消失）。本地 Turn 结束不再无条件置 idle，而是重新读取状态，正确呈现随后的后台占用。
- 后台广播：`handlers_goal_triggers` 取得租约后广播 busy；`_release_lease` 在提交并关闭 Session 后广播 idle；抢锁失败不广播，避免覆盖实际持有者状态。
- 状态机：idle ⇄ running（前台 Turn，cancellable）/ busy（后台 GoalDecision，不可中断）；取消为 running → 登记 `cancel_requested_at` → 执行者收口为 cancelled，锁仅在收口事务内原子释放；已完成 Turn 的停止请求返回 completed。
- 关键测试：唯一 Thread（stale 拒绝无写入 / 缺省复用 / 内部来源不受限）、流式 202/409 结构化错误、跨端 HTTP 中断与历史消失、解析期间取消不写记忆、GoalDecision 占用广播、完成后释放锁。
- 剩余风险：无浏览器/真机自动化；同步 `/api/chat` 取消为节点边界停止且不保留部分工具快照；本地遗留 `%TEMP%/aiive_test_*.db` 需用独立 TMPDIR 复跑。


### V63.2 WebSocket 重连历史恢复与聊天错误契约统一（2026-11-10）

- 基线：`8bc2c03`，main，开始时工作区干净；未修改 WebSocket 后端协议、ConversationState、TurnExecutionService、Thread Lease、取消语义、表结构或迁移，未新增轮询或 turn_id 重试机制。
- 连接恢复：Web `connect()` 与 Android `subscribeThreadEvents.connect()` 每次创建连接时重置局部 `receivedInitialStatus=false`。第一次 `execution_status` 标记 initialSnapshot，先正常应用状态，再对 idle/running/busy 均请求历史；Android 回调新增 `{ initialSnapshot: boolean }` 元数据。
- 历史同步：首快照与远端 running/interrupting→idle 合并判断，每个事件最多发起一次历史请求；直接复用 Web `loadRemoteMessages(threadId)` 和 Android `refreshHistory()`，服务器历史覆盖本地（包括空历史），不做数量比较或正文去重。Web 原有 new_message 的 event_id 去重保留。
- 本地流保护：`inFlightRef` / `streamingRef` 为 true 时只置 `historyRefreshPendingRef`。本地 Turn finally 先清除本地流式状态，再消费标记、刷新一次；多次连接快照合并为一个布尔标记。Web 系统指令同样使用 inFlightRef，其 finally 也处理待刷新标记。
- 错误契约：同步 `_raise_turn_conflict_error` 仅 `turn_in_progress` 为 HTTP 202 / retryable=true；stale_thread_id、turn_cancelled、turn_interrupted、idempotency_key_mismatch 及未知非 Busy 冲突均 HTTP 409 / false。检查发现 SSE 路由的异常兜底也有全 true 问题，已采用相同判断；其余响应字段、错误码和状态码保持现有结构，SSE HTTP 仍为 200。
- HTTP 测试：`test_chat_api.py` 新增 6 类参数化用例，mock execute_turn 抛 TurnConflictError，断言完整 detail，并 mock 流式异常逐项比对 SSE 的 code/status/retryable。该文件 **24 passed**。
- 自动化场景：`node scripts/verify_chat_reconnect.cjs` 使用 TypeScript AST 提取实际生产回调并执行，以 Mock WebSocket、历史 HTTP 和 React setter 离线回放（不是浏览器或真机测试）：
  - A：Web/Android 均 PASS。断线期间服务器完成一轮，重连 idle 后请求历史；即使本地条数与正文相同也替换为服务器 event_id，重复 idle 不重复请求，无重复消息。
  - B：Web/Android 均 PASS。重连 running 先同步历史/状态；interrupting 保留远端活跃标记，idle 再刷新；首 idle 同时满足远端完成条件时仅一次请求。另验证取消后空历史清除消息。
  - C：Web/Android 均 PASS。本地流期间连续两次重连快照不覆盖乐观消息、不请求历史；实际 finally 退出本地流后仅请求一次，待刷新标记复位。
  - 附加：首 busy 快照刷新、首帧认证、wss URL、每次重连标记复位、cleanup 不重连、Web 推送 event_id 去重均 PASS。
- 生产构建：`frontend` 与 `apps/android` 各运行 `npm run build`（tsc + vite build）均通过；仅验证 Android TypeScript/Web 资产生产构建，未打包或安装 APK。
- 静态检查：`.venv/Scripts/ruff.exe check backend/aiive --select F,E9` 通过（与 CI 规则一致）；`basedpyright --pythonpath .venv/Scripts/python.exe` 为 0 errors / 0 warnings / 0 notes。
- 后端全量单元测试：**1227 passed / 5 skipped / 29 warnings，227.28s**。命令：设置进程环境 `SHELL_BASH_PATH=E:/git/Git/bin/bash.exe`、`PYTHONUTF8=1` 后运行 `.venv/Scripts/python.exe -m pytest tests/unit/backend -q`；5 个跳过均为 Windows 不支持的 POSIX 部署脚本测试。29 个警告涉及 SQLAlchemy 事务/日期适配及未 await 的广播协程，未在本轮扩展修复。首次默认环境 16 failed / 1211 passed / 5 skipped，失败均为真实 Shell 用例，本机 PATH 的 bash 指向 WSL 启动器；使用已有 Git Bash 解决测试环境问题，不改仓库配置，Shell 专项另有 30 passed。最终进程清理报告 `tests/artifacts/validation/cleanup/36368/cleanup_report.json` 为 test_exitstatus=0 / status=cleaned / exists_after_cleanup=false。
- 本地证据（构建目录不提交）：`build/v632-chat-tests.log`、`build/v632-unit-tests-gitbash.log`、`build/v632-shell-gitbash.log`、`build/v632-basedpyright.log`。实现与离线验收完成，跨设备人工验收仍待执行。

人工跨设备验收（待执行，未声称完成真实浏览器或 Android 真机测试；仓库未配置 Playwright/WebDriver 场景）：

1. A：手机与电脑登录同一服务，确认同一 Thread；手机断网，电脑完成一轮问答，手机恢复网络。观察 WS 重连认证后的首 execution_status=idle，以及随后 GET `/api/threads/{threadId}/messages`；手机应显示完整用户消息与 Agent 回复各一条。
2. B：手机断网后电脑开始较长回复，回复结束前恢复手机网络；确认首 running 快照即加载当前历史并显示执行中。电脑完成或取消后，手机收到 idle 并再次 GET 历史；完成内容应与服务器一致，取消时被回滚的用户消息应消失。
3. C：手机本地发起流式回复，保持 SSE 可继续传输而单独断开并恢复 WS（调试代理或受控测试工具）；确认首快照不会立刻触发历史覆盖、Token 继续追加；Turn finally 结束后只执行一次待刷新请求。分别检查成功、取消和流式失败路径。

剩余风险：离线回放不覆盖真实网络、浏览器渲染及 Android WebView 生命周期；上述人工流程仍需设备实测。历史请求失败沿用现有处理，不新增重试/轮询；慢历史响应与后续事件的跨请求排序仍沿用现有机制，本轮未引入复杂状态协调。


### V64.1 全局上下文预算、来源感知溢出与历史 Token 密封（2026-11-10）

- 基线：`c52387e`，main，工作区初始干净。未新增数据库表/字段/迁移、Model Router、即时 LLM 压缩或跨来源评分公式；Forget / MemoryWrite / Thread Lease / 取消及 Outbox fencing 语义保持原有边界。
- 预算：ContextBudget 仅保留 model_context_window、reserved_output_tokens，hard_input_limit=两者之差。配置来自 AIIVE_LLM_CONTEXT_WINDOW / AIIVE_LLM_MAX_OUTPUT_TOKENS；旧 AIIVE_CONTEXT_BUDGET_JSON（包括非法 JSON）警告并忽略。删除 PartitionBudget、分区总额校验、弹性 Recent 及 soft_input_limit，保留 default/from_env 与 TurnExecution 缓存；ModelProfile / TokenCounter / 安全余量未改。
- 完整候选：一次收集完整 Turn（全部事件带 turn_id）、RetrievalHit（Memory/摘要共用原 final_score）、GraphRelationView、独立 SegmentSummary、结构化 WorkingState 与原角色顺序 Core Block。AUTO 在现有可见性/版本校验后回源加载完整内容，候选总量沿用调用方传入的全局 hard_input_limit，解决旧 200 字符预览对普通对话的隐性截断；显式 SEARCH/DEEP 的工具预算/预览限制不变。Core 投影来源指纹更新，旧裁剪缓存会由已有回源机制重建为完整 Block。
- Overflow：ContextOverflowPacker 是无数据库查询/无模型调用的纯逻辑模块。先整体 TokenCounter.count_messages，再依次删除最旧完整 Turn → 最低 final_score 的统一检索候选 → 低 score/远 hop Graph 关系 → 旧完整摘要 → WorkingState Artifact/低优先级循环 → 反向角色优先级 Core Block；每移除一项都重新渲染/真实计数。Stable Contract、当前消息、目标、关键约束、P0/P1 循环、待审批、未确认副作用/运行中工具和 interrupted_unknown 事实保护。保留工具调用与结果配对，必要内容仍超限则 ContextBudgetExceededError、不调用 LLM。
- 非 Token 安全边界保留：历史 max_pages/max_turns/max_events/max_raw_bytes、Memory top_k/路由上限、Graph hops/batch/relations、摘要 max_segment_summaries、Core 3 Block、ToolResultNormalizer Artifact 引用与 SINGLE_TOOL_RESULT_INLINE_LIMIT。WorkingState 去掉 token_budget 渲染参数，保留 MAX_LIST_ITEMS 与全部结构字段，复用 safe_boundary_snapshot 逐项来源可见性（来源不明仍 fail-closed，热上下文也复用该检查）。
- 诊断：ContextSnapshot 输入项也通过真实 TokenCounter 计数；PartitionReport 记录真实估计/安全 Token 与具体淘汰来源/原因，分区 hard_limit=None，仅 total 报全局硬上限。MemoryAccessTracker.touch、ContextSnapshot.injected_memory_ids 均从最终保留 Memory 生成；被淘汰候选和装配失败均不 touch。ForgetVisibilityService 先于候选注入，Packer 不重新查询或绕过屏蔽。
- 历史密封：_finalize_turn 完成 Turn、flush 当前回复和工具 Event 后，在同事务中调用 _mark_segment_pending_seal。ThreadState 仅读取指定 segment_id，token_budget=None 时保留所有访问上限；经 ToolResultNormalizer 后用 TokenCounter 汇总真实 estimated_tokens。阈值=min(AIIVE_SEGMENT_SEAL_TOKEN_THRESHOLD=64000, hard_input_limit*0.5)；达到阈值或 32 Turn 即写现有 pending_seal_at/sealed_by_turn。取消旧“总 Prompt 超 80%”密封；15 分钟 Idle 与 Scheduler/EpochManager/Outbox 流程保留。
- 摘要门禁：compaction.validate_compaction_prompt 使用生产 LiteLLMTokenCounter / ModelProfile / TokenSafetyConfig 校验 safe_tokens+配置最大输出<=窗口。Segment Handler 初次及追加 JSON 重试前均校验；确定性超限记录 compaction_prompt_overflow，立即使用 deterministic-extractive-fallback，继续 merge、Cover Check、source_hash 与 fenced commit，不对同一超限输入做 Worker 重试。Forget Summary 同门禁，超限直接 None，沿用 redacted 安全降级。Checkpoint 仍确定性聚合。

完整调用链：

`TurnExecution._load_context → ContextAssembler（可见完整候选 / 全量工具定义）→ ContextOverflowPacker → TokenCounter 整体硬门禁 → 最终 Memory touch + ContextSnapshot → AgentGraph → _finalize_turn/Event flush → ThreadState 当前 Segment 统计 → pending_seal → Scheduler → EpochManager freeze/CompactionInput → segment_sealing Outbox → 摘要初次/重试硬门禁 → LLM 或抽取降级 → merge/Cover Check/source_hash/fencing → Summary/Segment 原子提交`。

验收证据：

- 本地全量：`SHELL_BASH_PATH=E:/git/Git/bin/bash.exe`、`PYTHONUTF8=1`，`.venv/Scripts/python.exe -m pytest tests -q`：**1824 passed / 129 skipped / 5280 warnings，299.34s**（提交 `23f33b2` 后完整复跑，包含新增 PG 文件的默认跳过）。跳过为默认关闭的 PG 专项与 5 个 Windows 不支持的 POSIX 部署脚本；新 PG 测试另在真实专项运行。警告为现有 SQLite 日期适配、SQLAlchemy 事务与未 await 广播协程，未扩大到本轮之外修复。
- 真实 PostgreSQL：启动本机已有 Docker Desktop，`AIIVE_PG_TESTS=1` 运行 `pytest tests/integration/pg -q`，一次性 pgvector/ParadeDB 容器：**111 passed / 13 skipped，220.35s**。13 项均依赖本地真实 Embedding 服务；新增 5 项 V64.1（初次/重试超限降级提交、Token/32 Turn/跨 Segment 隔离）全部通过；测试容器已由 fixture 清理。
- 定向测试：Overflow/Assembler/ThreadState 新旧回归 **38 passed**，最终预算/Overflow/完整来源与快照复验 **44 passed**；普通 AUTO Memory / 历史摘要可超原 1200/800 配额，Core 完整 Block 可超 600，且 Forget Tombstone 屏蔽仍生效。四窗口 32K/50K/256K/1M、旧 JSON 弃用、小窗口、整 Turn/工具 JSON 完整性、最终 touch、历史独立触发、摘要初次/重试与 Forget 超限无调用均有断言。
- 静态检查：Ruff `check backend/aiive --select F,E9` 通过；basedpyright（仓库 .venv）0 errors / 0 warnings / 0 notes。
- 本地日志（build 不提交）：v641-all-final.log、v641-committed-tests.log、v641-pg-final.log、v641-new-tests.log、v641-retrieval-tests.log、v641-types.log、v641-final-focused.log。进程数据库清理由根 pytest_sessionfinish 验证；真实 PG 使用一次性容器，不连接或清理用户生产库。
- GitHub CI：实现提交 `23f33b2` 的 [CI #82](https://github.com/juzixiaoren/AIive/actions/runs/42847159230) **9/9 jobs success**：单元/关键流程、逐文件隔离、集成、真实 PostgreSQL、Ruff、basedpyright、前端生产构建、Alembic 空库 upgrade/check、Docker Compose 冒烟均成功。验收回执提交仅更新本段证据，使用 `[skip ci]` 避免重复执行已经通过的代码检查。
- 清理证据：最终本地进程 `tests/artifacts/validation/cleanup/41972/cleanup_report.json` 为 test_exitstatus=0、status=cleaned、exists_after_cleanup=false；`docker ps` 确认一次性测试容器已清理。

修改文件：

- 运行时：runtime/context_budget.py、context_overflow.py（新增）、context_assembler.py、thread_state.py、turn_execution.py、working_state.py、compaction.py。
- 来源与配置：config.py；memory/core_memory_projection.py、recall_config.py、recall_models.py、recall_fusion.py、graph_types.py、graph_expansion.py；retrieval/unified_retriever.py。
- Worker：worker/outbox_handlers.py、handlers_forget.py。
- 新增测试：tests/unit/backend/test_context_overflow.py、tests/integration/backend/test_context_v641.py、tests/integration/pg/test_pg_context_v641.py；迁移既有 ContextAssembler、GoalQuestion、ThreadState、Recall 测试夹具与断言。
- 文档：CODE_MAP、current_phase、architecture/Phase_1、architecture/Phase_3、AIive_核心架构与实现深度分析。

剩余风险：候选完整装配可能增加 CPU 计数/Prompt 大小；现有数据库访问上限保留，所以“完整”指访问边界内的完整结构单元。极小窗口下必要契约/工具/执行事实本身仍可能装不下，此时明确拒绝，不截断或额外调用摘要模型。历史超过资源读取上限时统计仍受有界读取约束；32 Turn 和 15 分钟 Idle 兜底保留。未做真实外部 LLM/Embedding 调用或百万窗口性能压测，配置窗口需与实际模型能力匹配。


## V64.2 上下文价值策略与可解释全局 Packing（2026-11-11）

- 基线：main `c30a23e`。状态：实现、文档与离线验收完成；真实 PostgreSQL 验收受 Docker Desktop 启动故障阻塞，未宣称通过。
- 新增 `runtime/context_policy.py` 的 ContextCandidateDecision、policy_version、来源内百分位与字典序规则；ContextCandidates 固定原始排名，以 T0–T3 / percentile / 估计 Token / 稳定键全局淘汰。Retrieval final_score、Graph score 各自独立排序；History 整 Turn、Summary 重要字段提升 T2、Core 保留 human_identity 最后删除。T3 与既有副作用/工具保护不变。
- single/conflicted Graph 按主体/谓词/Scope 原子分组，组按最强成员等级/百分位与合计 Token 参与决策。每删一组重新 render 完整 Prompt，真实 TokenCounter Hard Gate；无额外 LLM 评分。
- ContextSnapshotData.selection_trace 经 AgentGraph._assemble_agent_result 传递，由 _rotate_snapshot 写现有 meta.selection_trace，包含版本、全部可见候选的元数据、最终 selected/exclusion_reason 和 eviction_order。不复制被淘汰内容，不改变 full_contents、RetrievalCandidate.selected、Memory touch 或 Forget 可见性语义；无表/迁移变更。WorkingState version/epoch_id 不作为 Prompt 候选。
- 相关定向：**35 passed，11.07s**；全量：**1835 passed / 131 skipped / 0 failed，307.02s**（5280 条既有警告）；全量使用 `SHELL_BASH_PATH=E:/git/Git/bin/bash.exe`、`PYTHONUTF8=1`。首轮未设置 Bash 路径的 17 项环境失败在完整复跑中全部消失。
- Ruff 全 backend `--select F,E9` 通过；策略/Overflow/相关单测与 PG 文件默认规则通过；basedpyright **0 errors / 0 warnings / 0 notes**；git diff --check 通过。
- PostgreSQL：显式 `AIIVE_PG_TESTS=1 pytest tests/integration/pg -q`，**126 skipped，45.69s**。Docker Desktop 的 Inference manager 在 `AppData/Local/Docker/run/dockerInference` socket 初始化时崩溃，未提供隔离测试 URL；未取得真实 PG 通过证据。新增 `test_pg_context_v642.py` 两项验证最终 touch/trace 和既有 Snapshot JSON 持久化，待环境恢复执行。不改 Docker 配置、不接触生产库。
- 全量/定向/PG 进程清理报告均为 cleaned、exists_after_cleanup=false。日志：`build/v642-full-final.log`、`v642-final-focused.log`、`v642-pg-final.log`、`v642-ruff-final.log`、`v642-types-final.log`；详细调用链、淘汰例、Snapshot JSON 和覆盖表见 `docs/reports/2026-11-11_V642_context_policy.md`；架构说明已同步 CODE_MAP、Phase_1、ARCHITECTURE_AND_REVIEW。


## P1 统一工作中心（2026-11-12）

基线 main `8038182`（原探索基线 `5a7e799` 之后的安全契约与委派卡片补充）。
本轮完成 Backend、Web、Android 的同一业务闭环，不新增表或另一套执行状态。

- Web TaskCenterPage 使用 Inbox/WorkView/Timeline，四分类游标分页、全部既有操作、
  真实 Goal 进度、设备关系、报告/成果/证据与可折叠 Action 摘要。
- Android 独立移动 UI，底部对话/工作切换保留 ChatApp 与 SSE，Thread 订阅互斥；
  官方 Filesystem/Share/App 8.x 完成分段文件、校验保存分享及恢复前台同步。
- 公共 `shared/work` 模块负责 API、操作语义、刷新竞争与去重、文件名与 MIME 策略；
  聊天工作卡片与提醒卡片分离，历史恢复复用同一映射，普通 task_event 不生成聊天消息。
- Backend Timeline 补齐真实 Goal 事件，内部问题账本不进入 Timeline；计划编辑合并
  未编辑的步骤字段，保留脱敏视图省略的触发条件。所有读取仍受 Forget 校验。
- 本地全量 Python：1943 passed / 134 skipped / 5280 既有 warnings，440.23 秒。
  PostgreSQL：116 passed / 13 skipped，301.66 秒，包含 SAVEPOINT；跳过外部 Embedding 项。
  具体检查、CI 与边界见 [验收报告](../reports/2026-11-12_P1_work_center.md)。
- CI 在现有前端 Job 加入 Android TypeScript/Vite 与跨端 Node 测试，保持 9 Job。
  按用户要求不使用浏览器测试、不添加人工三端联调或推送基础设施。


## P2 跨端聊天附件（2026-11-12）

Backend / Web / Android 同一 Part 实现：内容寻址上传、认证下载、原消息关联、
全局预算、多模态模型配置、历史附件、Forget 来源、取消/TTL 清理。
验证进度及限制见 `docs/reports/2026-11-12_P2_chat_attachments.md`。


## P1/P2 最终收尾（2026-11-12）

修复 execution_status 完成时刷新、刷新分区失败隔离、附件实际来源、中央 Forget Scrub/Verify、
提交后对象删除、历史幂等补偿、安全草稿恢复、累计配额、历史有界查询与上传 Event 清理。
没有使用浏览器测试。验证结果见 P1/P2 报告新增收尾章节；原有功能与 CI 门禁保留。


## P1/P2 最终两项验收

批量 Shield 检查现在以附件自身 Thread/上传时间匹配 thread、time_range、all_user_data 和 cutoff；
查询异常关闭读取。原 Event/Turn/Tombstone 校验保留，原始附件读取排除 memory_only 屏蔽。
GC 内部 Operation 通过真实 VerifyReport 与删除/共享引用复查后才填写 verified_at 并收口；
无对象 Action 和旧的 purged-but-unverified Operation 同样可以验证收口，失败保留重试。
普通用户 Forget 异步机制不变，无新表或产品功能。

附件/GC 聚焦 42 passed；中央字段覆盖测试保留；Node 工作/传输 20 项、聊天 4 项通过，
Web/Android 生产构建通过，Ruff 和 basedpyright 通过。新增 PostgreSQL GC/Retention 契约。
未使用浏览器测试。完成最终提交的 9 项 CI 后关闭 P1/P2，不继续扩展阶段范围。
