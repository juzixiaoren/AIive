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
- P2 supervisor 真正托管进程（spawn/stop/restart + 端口切换 + slot_switch_events）
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
