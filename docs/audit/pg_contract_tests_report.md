# PostgreSQL 核心契约测试报告

- 版本：V34.1（V34 安全门之后的数据库一致性补齐）
- 日期：2026-10-22
- 目标：在真实 PostgreSQL 上验证事务、锁、幂等与并发语义，覆盖 SQLite 无法验证的
  advisory lock / SKIP LOCKED / FOR UPDATE / deadletter 原子性。

## 1. 测试环境（隔离，不连生产库）
- 本地：Docker 启动一次性 `pgvector/pgvector:pg16` 容器（本机端口 55432–55435，`--rm`），
  会话结束自动移除；每个用例前 `TRUNCATE ... RESTART IDENTITY CASCADE` 全表清理。
- CI：新增独立 Job `postgres-contract`，使用 `pgvector/pgvector:pg16` service + `AIIVE_PG_TESTS=1`。
- 若设置 `AIIVE_PG_TEST_URL`，直接复用该库（调用方负责隔离）。
- 不调用任何真实外部 API；向量能力关闭。

## 2. 新增契约测试（`tests/integration/pg/test_pg_contracts.py`，8 个）
| 用例 | 验证的契约 |
|---|---|
| `test_pg_schema_is_postgresql` | 环境自检：确实运行在 PostgreSQL，关键表存在 |
| `test_advisory_lock_is_acquired_on_postgresql` | `_acquire_lock` 在 PG 上真正持有 `pg_advisory_xact_lock`（非 SQLite no-op）：第二个会话取同一锁会阻塞至 `statement_timeout` |
| `test_concurrent_same_key_yields_single_active_record` | 8 线程并发写同一 canonical_key：advisory lock 串行化，最终恰好 1 条 active+valid 记录；同一 `idempotency_key` 恰好 1 条 proposal |
| `test_concurrent_claims_never_duplicate` | 6 Worker 并发 `claim_one` 领取 30 个 pending job：无重复领取，30 个各被领取一次 |
| `test_skip_locked_skips_row_locked_by_other_transaction` | 事务 A 锁定首行后，事务 B 的 `FOR UPDATE SKIP LOCKED` 跳过该行拿到下一行且不阻塞（无 SKIP LOCKED 会超时） |
| `test_forget_cascade_exception_leaves_no_half_state` | 注入 `_enqueue_next_stage` 异常：cascade 回滚，batch 不进入 done，不产生 next-stage job |
| `test_forget_deadletter_is_atomic_across_job_stage_operation` | 重试耗尽 deadletter：`OutboxJob.status=deadletter` + `ForgetStageRun.status=deadletter` + `ForgetOperation.status=shielded_deadletter` 原子一致 |
| `test_rollback_leaves_no_memory_or_projection_residue` | 真实写入路径（`MemoryWriteService.write`）后回滚：不残留 MemoryRecord，也不残留同事务入队的投影 OutboxJob |

## 3. 基础设施
- `tests/integration/pg/conftest.py`：Docker/URL 供给、`CREATE EXTENSION vector`、`create_all`、
  全表 TRUNCATE、把 `SessionLocal()` 重定向到 PG（复用 `tests/support.py` 分发器）。
- `pg` marker 已注册；未设置 `AIIVE_PG_TESTS=1` 时收集阶段直接 skip（0.09s，不启动容器），
  不拖慢默认快速测试。
- `.github/workflows/ci.yml` 新增 `postgres-contract` Job。

## 4. 实际运行结果
- PG 专项：`8 passed`（连续 3 次运行均通过，16–18s/次，无 flaky）。
- 默认全量：`884 passed, 8 skipped, 0 failed`（116.66s）；8 skipped 即 PG 专项（未启用）。
- 静态检查：`ruff check tests/integration/pg` 通过。

## 5. 发现与修复
- **未发现产品 Bug**：契约均成立。
- 测试构建过程中修正了两处测试自身问题（非产品缺陷）：
  1. 初始 proposal 使用非法 `memory_type="fact"`，被 Gate 拒绝；改为合法 `user_profile` 且 `confidence=0.9`。
  2. `ForgetStageRun` 与 `OutboxJob` 无 ORM relationship，同批 insert 时 FK 顺序不确定；显式 `flush()` 父行后插入。

## 6. 仍存在的数据库一致性风险
- PG 测试用 `Base.metadata.create_all` 建表，而非 Alembic 迁移；schema 与迁移历史可能漂移（`alembic` CI Job 单独覆盖迁移）。
- 并发用例为有限线程（6–8）与单键工作负载；未压测多键交叉死锁、长事务、连接池耗尽。
- advisory lock 键为 sha256 前 64 位，理论碰撞未测试。
- 仅覆盖 outbox claim 的 SKIP LOCKED；`tool_operations` 等其他领取路径未覆盖。
- Forget deadletter 用例直接调用 `_retry_or_deadletter`，未跑完整 poll 循环；跨进程重启的多阶段恢复未覆盖。
- 仅验证 PostgreSQL 16 + pgvector；其他版本未验证。
