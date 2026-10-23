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
