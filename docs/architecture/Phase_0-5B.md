# Phase 0.5B：Outbox Claim/Lease/Fencing、MemoryIngestionRun、异步提取启用（v6 最终版）

> **状态**：方案（待确认） | **禁止**：修改任何代码
>
> **前置**：Phase 0.5A 已部署（TurnRecord 表存在）。
> 本 Phase 负责 Outbox claim/lease/fencing、MemoryIngestionRun 批次幂等、capability flag 阻止无效 Job、allowlist 白名单消费、旧 Job 分类迁移、正式启用异步 memory_extraction。

---

## 架构分层

```
┌──────────────────────────────────────────────────┐
│ OutboxWorker（Outbox 基础设施层）                  │
│ - claim_one() / finalize_job() / retry_or_deadletter │
│ - retry_later() / CLAIM_LOST 处理                 │
│ - Heartbeat 续租                                  │
│ - 根据 HandlerResult 决定 Outbox 状态迁移          │
│ - 不参与业务 Session 管理                         │
└────────────────────┬─────────────────────────────┘
                     │ 调用 Handler，接收 HandlerResult
┌────────────────────▼─────────────────────────────┐
│ MemoryExtractionJobHandler（业务层）              │
│ - Phase A: 自己管理 Session → commit             │
│ - Phase B: 无 Session，LLM 调用                   │
│ - Phase C: 自己管理 Session → commit             │
│ - 返回 HandlerResult 通知 Worker 如何更新 Outbox  │
└──────────────────────────────────────────────────┘
```

---

## A. Handler 返回协议

Worker 需要区分四种语义，不能通过普通 return 混为一谈。所有 Handler 返回 `HandlerResult`：

```python
@dataclass(frozen=True)
class HandlerResult:
    """Handler 执行结果，指导 Worker 如何更新 OutboxJob。"""
    outcome: "HandlerOutcome"
    reason: str = ""
    retry_available_at: datetime | None = None  # RETRY_LATER 时使用
    ingestion_run_id: str = ""  # 用于 max_retries 后按 Run ID 标记 deadletter


class HandlerOutcome(StrEnum):
    """Handler 执行结果枚举。"""
    COMPLETED = "completed"
        # 业务已完成（succeeded、幂等命中、空结果）
        # → Worker finalize OutboxJob → completed

    RETRY_LATER = "retry_later"
        # 当前无法执行（running + 其他有效 token）
        # → Worker 设置 OutboxJob available_at，退回 pending

    NON_RETRYABLE = "non_retryable"
        # 不可重试（deadletter Run、source Turn 不存在/不匹配、
        #   payload 无效、unsupported schema_version）
        # → Worker finalize OutboxJob → deadletter

    RETRYABLE_ERROR = "retryable_error"
        # 可重试错误（Extractor 异常、DB 写入失败）
        # → Worker retry_or_deadletter

    CLAIM_LOST = "claim_lost"
        # Phase C 中 execution_token 不匹配或已被接管
        # → Worker 不得 finalize、retry 或修改 OutboxJob 状态
        #   仅从 Registry 移除 claim，由接管 Worker 负责后续
```

### Worker 根据 HandlerResult 决定 Outbox 状态

```python
def poll(self, max_jobs: int = 10):
    for _ in range(max_jobs):
        claimed = self.claim_one()
        if claimed is None:
            break

        active = ActiveClaim(
            job_id=claimed.id, claim_token=claimed.claim_token,
            worker_id=self._worker_id, started_at=datetime.now(timezone.utc),
        )
        self._claims.add(active)

        try:
            handler = self._handlers.get(claimed.job_type)
            if handler is None:
                self._deadletter_job_and_ingestion_run(
                    claimed, "",
                    f"No handler: {claimed.job_type}",
                    terminal_reason="unknown_job_type",
                )
                continue

            # Handler 自己管理业务 Session，Worker 不介入
            result: HandlerResult = handler(claimed)

            if result.outcome == HandlerOutcome.COMPLETED:
                self._finalize_job(claimed, "completed", result.reason)
            elif result.outcome == HandlerOutcome.RETRY_LATER:
                self._retry_later(claimed, result.retry_available_at, result.reason)
            elif result.outcome == HandlerOutcome.NON_RETRYABLE:
                self._deadletter_job_and_ingestion_run(
                    claimed,
                    ingestion_run_id=result.ingestion_run_id,
                    error=result.reason,
                    terminal_reason=result.terminal_reason,
                )
            elif result.outcome == HandlerOutcome.RETRYABLE_ERROR:
                self._retry_or_deadletter(
                    claimed, result.reason,
                    ingestion_run_id=result.ingestion_run_id,
                )
            elif result.outcome == HandlerOutcome.CLAIM_LOST:
                # 不做任何 Outbox 状态变更
                # 接管 Worker 负责后续
                logger.warning("Claim lost: job_id=%s token=%s",
                               claimed.id, claimed.claim_token)

        except FencingViolationError:
            # 基础设施 fencing 失败 → 按 CLAIM_LOST 处理，不修改 OutboxJob
            logger.warning(
                "Fencing violation in infrastructure: job_id=%s token=%s",
                claimed.id, claimed.claim_token,
            )
        except Exception as e:
            # Handler 本身崩了（未捕获异常）→ 可重试
            self._retry_or_deadletter(claimed, str(e))
        finally:
            self._claims.remove(claimed.id, claimed.claim_token)
```

### RETRY_LATER 实现

```python
def _retry_later(self, claimed: ClaimedJob,
                 available_at: datetime | None, reason: str):
    """将 Job 退回 pending，设 available_at 延迟重试。"""
    db = SessionLocal()
    try:
        affected = db.query(OutboxJob).filter(
            OutboxJob.id == claimed.id,
            OutboxJob.claim_token == claimed.claim_token,
            OutboxJob.locked_by == self._worker_id,
            OutboxJob.status == "running",
        ).update({
            OutboxJob.status: "pending",
            OutboxJob.error_message: reason[:500],
            OutboxJob.locked_by: None,
            OutboxJob.claim_token: None,
            OutboxJob.lease_expires_at: None,
            OutboxJob.available_at: available_at or (
                datetime.now(timezone.utc) + timedelta(seconds=30)
            ),
            OutboxJob.updated_at: datetime.now(timezone.utc),
        })
        if affected != 1:
            raise FencingViolationError(
                f"retry_later fencing: job_id={claimed.id} token={claimed.claim_token}"
            )
        db.commit()
    finally:
        db.close()
```

### 所有 Outbox 状态变更必须检查 affected_rows

`finalize_job`、`retry_or_deadletter`、`retry_later`、Heartbeat 的所有 `UPDATE` 操作都必须检查 `affected` 值：

| 操作 | 预期 affected | 不符合时 |
|------|-------------|---------|
| finalize_job (running → completed/deadletter) | 1 | `FencingViolationError` |
| retry_or_deadletter (running → pending/deadletter) | 1 | `FencingViolationError` |
| retry_later (running → pending) | 1 | `FencingViolationError` |
| Heartbeat 续租 | ≥0 | `affected==0` → 标记 lost |
| _mark_ingestion_failed_fencing | ≥0 | 仅日志警告 |
| _mark_ingestion_run_deadletter_by_id | ≥0 | 仅日志警告 |

---

## B. 事务所有权

### 原则

- **OutboxWorker** 只负责 OutboxJob 的 claim / finalize / retry。这些操作使用独立短 Session。
- **MemoryExtractionJobHandler** 自己管理 Phase A 和 Phase C 的业务 Session。Worker 不替业务 Handler commit。
- Handler 接口统一：接收 `ClaimedJob`，返回 `HandlerResult`。不接收 `Session` 参数。

### Handler 签名

```python
# 旧接口（v5，矛盾）
def handle_memory_extraction(
    db: Session, payload: dict, trace_id: str | None, claimed: ClaimedJob,
) -> None:

# 新接口（v6，统一）
def handle_memory_extraction(claimed: ClaimedJob) -> HandlerResult:
```

### 所有 Handler 必须使用统一接口

Allowlist 中所有 job_type 的 Handler 必须遵循统一签名 `(ClaimedJob) -> HandlerResult`。这包括 `core_memory_refresh`：

```python
def handle_core_memory_refresh(claimed: ClaimedJob) -> HandlerResult:
    """core_memory_refresh 使用统一 Handler 接口。

    自行管理业务 Session，不依赖 Worker 传入。
    """
    payload = claimed.payload
    memory_id = payload.get("memory_id", "")
    record_version = payload.get("record_version", 0)

    db = SessionLocal()
    try:
        from aiive.memory.core_memory_projection import CoreMemoryProjection
        from aiive.memory.recall_config import RecallConfig

        CoreMemoryProjection.refresh(db, memory_id, record_version, RecallConfig())
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, "core_memory_refreshed")
    except Exception as e:
        db.rollback()
        logger.exception("core_memory_refresh failed")
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, str(e))
    finally:
        db.close()
```

不得保留 `(db, payload, trace_id)` 旧签名供新 Worker 调用。所有注册到 Worker 的 Handler 必须统一返回 `HandlerResult`。

### HandlerRegistry 与 schema_version 预校验

```python
class HandlerRegistry:
    """Handler 注册表，包含每个 job_type 的 supported_schema_versions。"""

    def __init__(self):
        self._handlers: dict[str, Callable[[ClaimedJob], HandlerResult]] = {}
        self._supported_versions: dict[str, frozenset[int]] = {}

    def register(
        self, job_type: str, handler: Callable[[ClaimedJob], HandlerResult],
        supported_schema_versions: frozenset[int],
    ) -> None:
        self._handlers[job_type] = handler
        self._supported_versions[job_type] = supported_schema_versions

    def get(self, job_type: str) -> Callable[[ClaimedJob], HandlerResult] | None:
        return self._handlers.get(job_type)

    def is_schema_supported(self, job_type: str, schema_version: int) -> bool:
        versions = self._supported_versions.get(job_type)
        return versions is not None and schema_version in versions


# 注册
handler_registry = HandlerRegistry()
handler_registry.register("memory_extraction", handle_memory_extraction,
                           supported_schema_versions=frozenset({1}))
handler_registry.register("core_memory_refresh", handle_core_memory_refresh,
                           supported_schema_versions=frozenset({1}))
```

Worker 在分发 Handler 前统一校验 schema_version：

```python
# Worker poll() 中
if handler is None:
    self._deadletter_job_and_ingestion_run(
        claimed, "", f"No handler: {claimed.job_type}",
        terminal_reason="unknown_job_type",
    )
    continue

# ── Worker 统一预校验 schema_version ──
if not self._registry.is_schema_supported(claimed.job_type, claimed.schema_version):
    self._deadletter_job_and_ingestion_run(
        claimed, "",
        f"Unsupported schema_version={claimed.schema_version} "
        f"for job_type={claimed.job_type}",
        terminal_reason="unsupported_schema_version",
    )
    continue

result: HandlerResult = handler(claimed)
```

### 各层 Session 归属

| 操作 | Session 所有者 | 生命周期 |
|------|--------------|---------|
| claim_one() | Worker | 独立短 Session，claim 成功后 commit 并关闭 |
| finalize_job() | Worker | 独立短 Session，更新后 commit 并关闭 |
| retry_or_deadletter() | Worker | 独立短 Session |
| retry_later() | Worker | 独立短 Session |
| Heartbeat 续租 | Heartbeat 线程 | 独立短 Session |
| Handler Phase A | Handler | Handler 创建/commit/关闭 |
| Handler Phase B | 无 | N/A |
| Handler Phase C | Handler | Handler 创建/commit/关闭 |
| _mark_ingestion_failed() | Handler（独立） | 独立短 Session |

### 禁止事项

- Handler 不能依赖 Worker 传入的 Session（根本没有传入）
- Handler 的 Phase A 和 Phase C 使用各自独立的 Session，绝不跨阶段共享
- Worker 不调用 `commit()` 或 `rollback()` 对任何业务 Session

### MemoryExtractionJobHandler 流程

```python
def handle_memory_extraction(claimed: ClaimedJob) -> HandlerResult:
    payload = claimed.payload
    user_message = payload.get("user_message", "")
    reply = payload.get("reply", "")
    thread_id = payload.get("thread_id", "")
    source_turn_record_id = payload.get("source_turn_record_id", "")

    if MemoryExtractionPolicy.should_skip_system_message(user_message):
        return HandlerResult(HandlerOutcome.COMPLETED, "system_message_skipped")

    # ════════════════════════════════════════════════
    # Phase A: 短事务 —— 幂等预检查 + source Turn 验证
    # Handler 自己创建并管理 Session
    # ════════════════════════════════════════════════
    db_a = SessionLocal()
    try:
        # Step A1: 验证 source Turn + schema_version
        validated_source = _validate_source_turn(
            db_a, source_turn_record_id, thread_id, claimed,
        )
        # 返回 ValidatedExtractionSource 或抛 NonRetryableError

        # Step A2: IngestionRun 幂等创建 + 状态检查
        irun = _resolve_ingestion_run(
            db_a, source_turn_record_id, claimed.claim_token,
        )
        # _resolve_ingestion_run 返回 IngestionRunResolution 或抛对应异常

        if irun.decision == "already_succeeded":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.COMPLETED, "already_succeeded",
                                 ingestion_run_id=irun.run_id)

        if irun.decision == "deadletter":
            db_a.rollback()
            return HandlerResult(HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                                 ingestion_run_id=irun.run_id,
                                 terminal_reason="ingestion_run_deadletter")

        if irun.decision == "busy":
            db_a.rollback()
            return HandlerResult(
                HandlerOutcome.RETRY_LATER,
                reason="running_with_valid_token",
                retry_available_at=irun.lease_expires_at,
            )

        # irun.decision == "acquired" or "takeover"
        # Phase A commit running 状态
        db_a.commit()
        execution_token = claimed.claim_token
        run_id = irun.run_id

    except NonRetryableJobError as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.NON_RETRYABLE, str(e))
    except Exception as e:
        db_a.rollback()
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase A failed: {e}")
    finally:
        db_a.close()

    # ════════════════════════════════════════════════
    # Phase B: LLM 提取（事务外，无 DB Session）
    # ════════════════════════════════════════════════
    try:
        llm = _get_llm_client()
        extractor = UnifiedMemoryExtractor(llm)
        proposals = extractor.extract(
            user_message=user_message, reply=reply,
            trace_id=claimed.trace_id, thread_id=thread_id,
        )
    except Exception as e:
        # Extractor 异常 → 清理 running Run
        _mark_ingestion_failed_fencing(
            run_id=run_id, execution_token=execution_token,
            error=str(e),
        )
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Extractor failed: {e}",
                             ingestion_run_id=run_id)

    # ════════════════════════════════════════════════
    # Phase C: 短事务 —— 锁定 + 写入 + 标记成功
    # Handler 自己创建并管理 Session
    # ════════════════════════════════════════════════
    db_c = SessionLocal()
    try:
        # ── Step C1a: 双重 fencing — 先验证 OutboxJob ──
        outbox_c = db_c.query(OutboxJob).filter(
            OutboxJob.id == claimed.id,
        ).with_for_update().first()

        if outbox_c is None:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                 "outbox_job_not_found",
                                 ingestion_run_id=run_id)

        if not (
            outbox_c.claim_token == claimed.claim_token
            and outbox_c.locked_by == claimed.worker_id
            and outbox_c.status == "running"
            and outbox_c.lease_expires_at is not None
            and outbox_c.lease_expires_at > datetime.now(timezone.utc)
        ):
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                 "outbox_claim_expired_or_taken_over",
                                 ingestion_run_id=run_id)

        # ── Step C1b: 再验证 IngestionRun ──
        run_c = db_c.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
        ).with_for_update().first()

        if run_c is None or run_c.execution_token != execution_token:
            db_c.rollback()
            # token 不匹配或被接管
            if run_c is not None and run_c.status == "succeeded":
                return HandlerResult(
                    HandlerOutcome.COMPLETED, "already_succeeded_by_other",
                    ingestion_run_id=run_id,
                )
            if run_c is not None and run_c.status == "deadletter":
                return HandlerResult(
                    HandlerOutcome.NON_RETRYABLE, "deadletter_by_other",
                    ingestion_run_id=run_id,
                )
            return HandlerResult(
                HandlerOutcome.CLAIM_LOST, "ingestion_run_token_mismatch",
                ingestion_run_id=run_id,
            )

        if run_c.status != "running":
            db_c.rollback()
            if run_c.status == "succeeded":
                return HandlerResult(
                    HandlerOutcome.COMPLETED, "already_succeeded",
                    ingestion_run_id=run_id,
                )
            if run_c.status == "deadletter":
                return HandlerResult(
                    HandlerOutcome.NON_RETRYABLE, "run_deadletter",
                    ingestion_run_id=run_id,
                )
            return HandlerResult(
                HandlerOutcome.CLAIM_LOST, f"run_not_running: {run_c.status}",
                ingestion_run_id=run_id,
            )

        # Step C2: 构建 RunContext（turn_id 来自 validated_source）
        run_ctx = RunContext(
            thread_id=validated_source.thread_id,
            trace_id=claimed.trace_id or "",
            source="outbox_worker",
            turn_id=validated_source.turn_id,  # 已验证非空
        )

        writer = MemoryWriteService(db_c)

        if not proposals:
            # 空结果：标记 succeeded + proposal_count=0
            affected_empty = db_c.query(MemoryIngestionRun).filter(
                MemoryIngestionRun.id == run_id,
                MemoryIngestionRun.execution_token == execution_token,
                MemoryIngestionRun.status == "running",
            ).update({
                MemoryIngestionRun.status: "succeeded",
                MemoryIngestionRun.proposal_count: 0,
                MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
            })
            if affected_empty == 0:
                db_c.rollback()
                return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                     "empty_result_fencing_violation",
                                     ingestion_run_id=run_id)
            db_c.commit()
            return HandlerResult(HandlerOutcome.COMPLETED, "empty_extraction",
                                 ingestion_run_id=run_id)

        # Step C3: 原子写入批次
        writer.write_batch(
            proposals=proposals,
            ingestion_run=run_c,
            run_context=run_ctx,
        )

        # Step C4: 标记 succeeded
        affected = db_c.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
            MemoryIngestionRun.execution_token == execution_token,
            MemoryIngestionRun.status == "running",
        ).update({
            MemoryIngestionRun.status: "succeeded",
            MemoryIngestionRun.proposal_count: len(proposals),
            MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
        })
        if affected == 0:
            db_c.rollback()
            return HandlerResult(HandlerOutcome.CLAIM_LOST,
                                 "fencing_violation_in_finalize",
                                 ingestion_run_id=run_id)

        db_c.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, f"written_{len(proposals)}",
                             ingestion_run_id=run_id)

    except MemoryBatchWriteError as e:
        db_c.rollback()
        _mark_ingestion_failed_fencing(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Batch write failed: {e}",
                             ingestion_run_id=run_id)
    except Exception as e:
        db_c.rollback()
        _mark_ingestion_failed_fencing(run_id, execution_token, str(e))
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Phase C failed: {e}",
                             ingestion_run_id=run_id)
    finally:
        db_c.close()
```

---

## C. 三阶段异常路径

### Phase A 异常

| 异常 | 处理 | HandlerResult |
|------|------|--------------|
| source Turn 不存在 | db_a.rollback() | NON_RETRYABLE |
| TurnRecord.status ≠ completed | db_a.rollback() | NON_RETRYABLE |
| TurnRecord.thread_id 不匹配 | db_a.rollback() | NON_RETRYABLE |
| ClaimedJob.schema_version 不支持 | db_a.rollback() | NON_RETRYABLE |
| IngestionRun 已 succeeded | db_a.rollback() | COMPLETED |
| IngestionRun 已 deadletter | db_a.rollback() | NON_RETRYABLE |
| IngestionRun running + 有效 token | db_a.rollback() | RETRY_LATER |
| IngestionRun running + lease 过期 → 接管 | db_a.commit() | 继续 Phase B |
| DB 异常 | db_a.rollback() | RETRYABLE_ERROR |

关键：db_a 在 Phase A 结束时必定已 close。Phase B 不持有任何 Session。

### Phase B 异常

```python
except Exception as e:
    # ★ 必须使用独立短事务清理 running Run
    _mark_ingestion_failed_fencing(
        run_id=run_id,           # Phase A 中获取的 run.id
        execution_token=execution_token,  # claim_token
        error=str(e),
    )
    return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, f"Extractor failed: {e}")
```

`_mark_ingestion_failed_fencing` 必须：
1. 使用全新独立 Session
2. 按 `run_id + execution_token + status='running'` 精确更新
3. 设置 `status='failed'`, `execution_token=NULL`, 清空 `error_message`
4. 不再设置 `lease_expires_at`（IngestionRun 无独立租约，见 §D）

```python
def _mark_ingestion_failed_fencing(
    run_id: str, execution_token: str, error: str,
) -> None:
    """独立短事务：清理 Phase A 已 commit 的 running Run。

    仅在 Phase B Extractor 异常或 Phase C 写入失败后调用。
    使用 fencing 条件确保只清理当前 execution_token 对应的 Run。
    """
    db = SessionLocal()
    try:
        affected = db.query(MemoryIngestionRun).filter(
            MemoryIngestionRun.id == run_id,
            MemoryIngestionRun.execution_token == execution_token,
            MemoryIngestionRun.status == "running",
        ).update({
            MemoryIngestionRun.status: "failed",
            MemoryIngestionRun.execution_token: None,
            MemoryIngestionRun.error_message: error[:500],
            MemoryIngestionRun.completed_at: datetime.now(timezone.utc),
        })
        db.commit()
        if affected == 0:
            logger.warning(
                "_mark_ingestion_failed_fencing: no row matched "
                "run_id=%s token=%s (already taken over?)",
                run_id, execution_token,
            )
    except Exception:
        logger.exception("_mark_ingestion_failed_fencing itself failed")
    finally:
        db.close()
```

### Phase C 异常

| 异常 | 处理 | HandlerResult |
|------|------|--------------|
| OutboxJob 不存在、claim_token 不匹配、lease 过期 | db_c.rollback() | CLAIM_LOST |
| IngestionRun token 不匹配 + Run=succeeded | db_c.rollback() | COMPLETED |
| IngestionRun token 不匹配 + Run=deadletter | db_c.rollback() | NON_RETRYABLE |
| IngestionRun token 不匹配 + 其他 | db_c.rollback() | CLAIM_LOST |
| run.status=succeeded（Phase B 期间被完成） | db_c.rollback() | COMPLETED |
| run.status=deadletter | db_c.rollback() | NON_RETRYABLE |
| run.status=其他非 running | db_c.rollback() | CLAIM_LOST |
| MemoryBatchWriteError | db_c.rollback() + mark_failed | RETRYABLE_ERROR(+run_id) |
| DB 异常 | db_c.rollback() + mark_failed | RETRYABLE_ERROR(+run_id) |
| 空 proposals 标记 succeeded 时 affected=0 | db_c.rollback() | CLAIM_LOST |
| 非空 proposals 标记 succeeded 时 affected=0 | db_c.rollback() | CLAIM_LOST |

### 完整异常路径图

```
Phase A: db_a.open()
  ├─ source Turn 不存在 → rollback → NON_RETRYABLE
  ├─ unsupported schema_version → rollback → NON_RETRYABLE
  ├─ IngestionRun.succeeded → rollback → COMPLETED
  ├─ IngestionRun.deadletter → rollback → NON_RETRYABLE
  ├─ IngestionRun.running+valid → rollback → RETRY_LATER
  ├─ IngestionRun.running+expired → takeover → commit → 继续
  └─ 正常/接管 → commit → 继续
db_a.close()

Phase B: 无 Session
  ├─ Extractor 异常 → _mark_failed(独立Session) → RETRYABLE_ERROR(+run_id)
  └─ 正常 → proposals

Phase C: db_c.open()
  ├─ OutboxJob claim 失效或 lease 过期 → rollback → CLAIM_LOST
  ├─ IngestionRun token 不匹配 + Run=succeeded → rollback → COMPLETED
  ├─ IngestionRun token 不匹配 + Run=deadletter → rollback → NON_RETRYABLE
  ├─ IngestionRun token 不匹配 + 其他 → rollback → CLAIM_LOST
  ├─ run.status=succeeded → rollback → COMPLETED
  ├─ run.status=deadletter → rollback → NON_RETRYABLE
  ├─ run.status=其他非 running → rollback → CLAIM_LOST
  ├─ write_batch 成功 → commit → COMPLETED(+run_id)
  ├─ write_batch 异常 → rollback + _mark_failed → RETRYABLE_ERROR(+run_id)
  ├─ 空 proposals(affected=1) → commit → COMPLETED(+run_id)
  ├─ 空 proposals(affected=0) → rollback → CLAIM_LOST
  └─ succeeded UPDATE(affected=0) → rollback → CLAIM_LOST
db_c.close()
```

---

## D. 租约绑定

### 设计决策：IngestionRun 不维护独立时间租约

**问题**：v5 中 IngestionRun 有独立的 `lease_expires_at`，但 Outbox heartbeat 只续租 OutboxJob，不续租 IngestionRun。两个 lease 必然漂移。

**方案**：IngestionRun 不维护独立时间租约。其活跃性仅通过对应 OutboxJob 判断。

### 绑定模型

```
OutboxJob.claim_token == MemoryIngestionRun.execution_token
```

- OutboxJob 通过 Heartbeat 维护 `lease_expires_at`
- IngestionRun 的 `execution_token` 与当前 Outbox claim_token 相同时，该 Run 属于当前 Worker
- OutboxJob lease 过期 → 新 claim 接管 Job → 新 claim_token → 新 Worker 在 Phase A 看到旧 token → 接管 Run
- 不存在 IngestionRun 独自漂移的场景

### Phase A 中判断 busy 的方式

```python
def _resolve_ingestion_run(
    db: Session, source_turn_record_id: str, claim_token: str,
) -> IngestionRunResolution:
    """Phase A：创建/读取 IngestionRun 并决定执行权。

    不需要 IngestionRun 自己的 lease —— 通过 OutboxJob 的 claim 状态判断。
    如果当前 Run 有 execution_token 且该 execution_token 对应一个
    有效 lease 的 OutboxJob，则 busy；否则可接管。
    """
    # ON CONFLICT DO NOTHING
    stmt = pg_insert(MemoryIngestionRun).values(
        id=str(uuid.uuid4()),
        source_turn_record_id=source_turn_record_id,
        extractor_name="UnifiedMemoryExtractor",
        extractor_version="1.0",
        status="pending",
    ).on_conflict_do_nothing(
        index_elements=["source_turn_record_id", "extractor_name", "extractor_version"]
    )
    db.execute(stmt)

    run = db.query(MemoryIngestionRun).filter(
        MemoryIngestionRun.source_turn_record_id == source_turn_record_id,
        MemoryIngestionRun.extractor_name == "UnifiedMemoryExtractor",
        MemoryIngestionRun.extractor_version == "1.0",
    ).with_for_update().first()

    if run.status == "succeeded":
        return IngestionRunResolution(decision="already_succeeded")

    if run.status == "deadletter":
        return IngestionRunResolution(decision="deadletter")

    if run.status == "running" and run.execution_token:
        # 查询对应的 OutboxJob 是否仍然有效
        outbox_job = db.query(OutboxJob).filter(
            OutboxJob.claim_token == run.execution_token,
            OutboxJob.status == "running",
        ).first()

        if outbox_job and outbox_job.lease_expires_at:
            if outbox_job.lease_expires_at > datetime.now(timezone.utc):
                # OutboxJob 租约仍有效 → busy
                return IngestionRunResolution(
                    decision="busy",
                    lease_expires_at=outbox_job.lease_expires_at,
                )
        # OutboxJob 租约过期或不存在 → 可接管

    # ★ 在修改状态前保存原状态，用于正确判断 acquired vs takeover
    was_running = (run.status == "running")
    previous_status = run.status

    # 获取执行权
    run.status = "running"
    run.execution_token = claim_token
    run.started_at = datetime.now(timezone.utc)
    db.flush()

    return IngestionRunResolution(
        decision="acquired" if not was_running else "takeover",
        run_id=run.id,
        previous_status=previous_status,
    )


@dataclass
class IngestionRunResolution:
    decision: str  # already_succeeded | deadletter | busy | acquired | takeover
    run_id: str = ""
    lease_expires_at: datetime | None = None
    previous_status: str = ""
```

### Heartbeat 只续租 OutboxJob

Heartbeat 线程不需要感知 IngestionRun。它只续租 ActiveClaimRegistry 中的 OutboxJob。IngestionRun 的活跃性完全通过 execution_token 与 OutboxJob.claim_token 的一致性判断。

### 数据模型修正：MemoryIngestionRun 移除 `lease_expires_at`

```python
class MemoryIngestionRun(Base):
    __tablename__ = "memory_ingestion_runs"

    id: str                           # PK, uuid4
    source_turn_record_id: str        # FK → turn_records.id
    extractor_name: str
    extractor_version: str
    status: str                       # pending | running | succeeded | failed | deadletter
    execution_token: str | None       # 复用 Outbox claim_token
    # lease_expires_at 已移除 —— 通过 OutboxJob 判断活跃性
    started_at: datetime | None
    completed_at: datetime | None
    proposal_count: int               # default=0
    error_message: str | None
    created_at: datetime

    __table_args__ = (
        UniqueConstraint("source_turn_record_id", "extractor_name", "extractor_version"),
    )
```

---

## E. WriteOutcome

### 禁止通过 reason 字符串判断

v5 中 `write_batch` 通过 `result.reason not in ("ignore", "reinforce_skip", "gate_reject")` 判断合法 no-op。这容易因字符串不一致（如 `"gate_reject: xxx"` vs `"gate_reject"`）产生 Bug。

### WriteOutcome 枚举

```python
class WriteOutcome(StrEnum):
    """单个 Proposal 写入结果枚举。"""
    WRITTEN = "written"               # 成功写入 MemoryRecord
    GATE_REJECTED = "gate_rejected"   # Gate 拒绝（合法审计结果）
    IGNORED = "ignored"               # ConflictResolver ignore（合法）
    REINFORCE_SKIPPED = "reinforce_skipped"  # 所有 source_events 已计数
    FAILED = "failed"                 # 数据库异常 / 状态机违反 / 不可执行


@dataclass
class WriteResult:
    """单个 Proposal 的写入结果。"""
    outcome: WriteOutcome
    operation: str = ""
    memory_id: str = ""
    state: str = ""
    reason: str = ""       # 仅用于日志/调试，不用于逻辑判断
    superseded_ids: list[str] = field(default_factory=list)
```

### write_batch 中的使用

```python
# 合法 no-op：不导致批次失败
_VALID_NOOP_OUTCOMES: frozenset[WriteOutcome] = frozenset({
    WriteOutcome.GATE_REJECTED,
    WriteOutcome.IGNORED,
    WriteOutcome.REINFORCE_SKIPPED,
})

for i, p in enumerate(proposals):
    p.ingestion_run_id = ingestion_run.id
    p.proposal_index = i
    p.source_turn_id = run_context.turn_id

    result = self._write_single_in_transaction(p, run_context)
    results.append(result)

    if result.outcome not in _VALID_NOOP_OUTCOMES and result.outcome != WriteOutcome.WRITTEN:
        raise MemoryBatchWriteError(
            f"Proposal {i} failed: outcome={result.outcome.value}, reason={result.reason}"
        )
```

### _write_single_in_transaction 返回 WriteOutcome

```python
def _write_single_in_transaction(
    self, proposal: MemoryProposal, run_context: RunContext
) -> WriteResult:
    gate_decision = self._gate.decide(proposal)
    if gate_decision.decision == "reject":
        self._persist_proposal(proposal, gate_decision, final_op="reject")
        return WriteResult(
            outcome=WriteOutcome.GATE_REJECTED,
            reason=gate_decision.reason,
        )

    existing_active = self._store.get_active_by_key_scope_locked(...)
    resolution = self._resolver.resolve(proposal, existing_active)

    if resolution.operation == "reinforce":
        result = self._execute_reinforce(...)
        # _execute_reinforce 内部处理 reinforce_skip
        return result
    elif resolution.operation == "ignore":
        self._persist_proposal(proposal, gate_decision, final_op="ignore")
        return WriteResult(outcome=WriteOutcome.IGNORED, reason=resolution.reason)
    # ... 其他分支
```

`_execute_reinforce` 中当所有 source_events 已存在时：

```python
if new_count == 0:
    self._persist_proposal(proposal, gate_decision, final_op="ignore", final_memory_id=existing.id)
    return WriteResult(
        outcome=WriteOutcome.REINFORCE_SKIPPED,
        reason="All source_events already counted",
    )
```

---

## F. source Turn 验证

### 调用 Extractor 前必须验证

在 Phase A 中，调用 Extractor（Phase B）之前，必须验证 source Turn 和 schema_version。验证失败 → `NonRetryableJobError`，不调用 LLM。

```python
def _validate_source_turn(
    db: Session, source_turn_record_id: str, expected_thread_id: str,
    claimed: ClaimedJob,
) -> "ValidatedExtractionSource":
    """Phase A：验证 source Turn 存在且有效，以及 payload schema_version。

    Returns:
        ValidatedExtractionSource（纯数据，Session 关闭后安全使用）

    Raises:
        NonRetryableJobError: 验证失败（不调用 Extractor）
    """
    # ── Schema version 验证 ──
    if claimed.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise NonRetryableJobError(
            f"Unsupported schema_version: {claimed.schema_version} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    # ── Source Turn 验证 ──
    if not source_turn_record_id:
        raise NonRetryableJobError("missing source_turn_record_id in payload")

    turn = db.get(TurnRecord, source_turn_record_id)
    if turn is None:
        raise NonRetryableJobError(
            f"TurnRecord not found: {source_turn_record_id}"
        )

    if turn.status != "completed":
        raise NonRetryableJobError(
            f"TurnRecord not completed: status={turn.status}"
        )

    if turn.thread_id != expected_thread_id:
        raise NonRetryableJobError(
            f"Thread mismatch: expected={expected_thread_id}, actual={turn.thread_id}"
        )

    if not turn.turn_id:
        raise NonRetryableJobError("TurnRecord.turn_id is empty")

    return ValidatedExtractionSource(
        turn_record_id=turn.id,
        thread_id=turn.thread_id,
        turn_id=turn.turn_id,
    )


# 常量定义
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


@dataclass(frozen=True)
class ValidatedExtractionSource:
    """Phase A 验证后的纯数据对象，Phase B/C 使用。

    不含 ORM 引用，Session 关闭后安全使用。
    """
    turn_record_id: str
    thread_id: str
    turn_id: str
```

### 禁止空 source_turn_id

Phase C 中 `run_ctx.turn_id` 来源于 `ValidatedExtractionSource.turn_id`，已经过非空验证。`MemoryProposal.source_turn_id` 永远不会是空字符串。`ValidatedExtractionSource` 的创建在 `_validate_source_turn` 中，该函数在返回前已对 `turn_id` 做了非空检查。

---

## G. Deadletter / Quarantine

### Outbox max_retries 耗尽 → 原子 deadletter（OutboxJob + IngestionRun 同一事务）

禁止先独立提交 IngestionRun.deadletter 再 finalize OutboxJob，存在旧 Worker 错误终止新 Worker Run 的竞态。必须使用单一事务原子完成。

```python
def _deadletter_job_and_ingestion_run(
    self, claimed: ClaimedJob, ingestion_run_id: str, error: str,
    terminal_reason: str = "",
):
    """原子 deadletter：OutboxJob + IngestionRun 在同一事务中。

    事务顺序固定：
    1. SELECT OutboxJob FOR UPDATE
    2. 验证 job_id、claim_token、worker_id、status=running
    3. fencing 失败 → rollback，不得修改 IngestionRun
    4. SELECT MemoryIngestionRun FOR UPDATE（如果 ingestion_run_id 非空）
    5. 如果状态为 failed/running → 更新为 deadletter，清空 execution_token
    6. 更新 OutboxJob 为 deadletter
    7. 一次 commit
    """
    db = SessionLocal()
    try:
        # ── 锁定并验证 OutboxJob ──
        job = db.query(OutboxJob).filter(
            OutboxJob.id == claimed.id,
        ).with_for_update().first()

        if job is None:
            db.rollback()
            logger.warning("_deadletter: OutboxJob not found: %s", claimed.id)
            return

        if not (
            job.claim_token == claimed.claim_token
            and job.locked_by == claimed.worker_id
            and job.status == "running"
        ):
            # fencing 失败 → 不得修改任何状态
            db.rollback()
            logger.warning(
                "_deadletter fencing violation: job_id=%s token=%s",
                claimed.id, claimed.claim_token,
            )
            return

        # ── 更新 OutboxJob → deadletter ──
        job.status = "deadletter"
        job.terminal_reason = terminal_reason or "max_retries_exhausted"
        job.error_message = error[:500]
        job.locked_by = None
        job.claim_token = None
        job.lease_expires_at = None
        job.updated_at = datetime.now(timezone.utc)

        # ── 如果有关联的 IngestionRun，原子标记 deadletter ──
        if ingestion_run_id:
            irun = db.query(MemoryIngestionRun).filter(
                MemoryIngestionRun.id == ingestion_run_id,
            ).with_for_update().first()

            if irun is not None and irun.status in ("running", "failed"):
                irun.status = "deadletter"
                irun.execution_token = None
                irun.error_message = f"Outbox max_retries exhausted: {error[:200]}"
                irun.completed_at = datetime.now(timezone.utc)

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("_deadletter_job_and_ingestion_run failed")
    finally:
        db.close()


def _retry_or_deadletter(self, claimed: ClaimedJob, error: str,
                         ingestion_run_id: str = "",
                         terminal_reason: str = ""):
    new_retry = claimed.retry_count + 1
    if new_retry >= claimed.max_retries:
        self._deadletter_job_and_ingestion_run(
            claimed, ingestion_run_id, error, terminal_reason,
        )
        return
    # ... 正常退避重试（使用 fencing UPDATE，affected!=1 → FencingViolationError）
```

### 删除旧的两事务实现

`_mark_ingestion_run_deadletter_by_id()` 后再单独 `_finalize_job()` 的两事务实现已删除。所有 deadletter 路径统一走 `_deadletter_job_and_ingestion_run`。Worker 中：

```python
elif result.outcome == HandlerOutcome.NON_RETRYABLE:
    self._deadletter_job_and_ingestion_run(
        claimed,
        ingestion_run_id=result.ingestion_run_id,
        error=result.reason,
        terminal_reason=result.terminal_reason,
    )
```

### Unknown / Disabled Job quarantine

#### 入队时拒绝（新代码路径）

```python
def enqueue(self, db: Session, job_type: str, payload: dict, ...) -> OutboxJob:
    if job_type not in ENABLED_OUTBOX_JOB_TYPES:
        raise ValueError(f"Rejected: job_type='{job_type}' not in allowlist")
    # ...
```

#### 迁移：处理所有类型的 deadletter/quarantine

迁移 SQL 必须处理**所有**不在 allowlist 中的 pending Job，而非只列出的四种：

```sql
-- Step: 隔离所有不在 allowlist 中的 pending Job
WITH affected AS (
    UPDATE outbox_jobs
    SET status = 'deadletter',
        terminal_reason = CASE
            WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete')
                THEN 'unsupported_handler'
            WHEN job_type IN ('memory_markdown_project', 'memory_cache_invalidate')
                THEN 'unsupported_handler'
            WHEN job_type IN ('memory_extraction', 'steward_extraction')
                THEN 'cancelled_legacy'
            ELSE 'quarantined_unknown'
        END,
        migration_batch_id = :'batch_id',
        original_status = status,
        error_message = 'quarantined: job_type not in allowlist',
        locked_by = NULL,
        claim_token = NULL,
        lease_expires_at = NULL,
        updated_at = NOW()
    WHERE status = 'pending'
      AND job_type NOT IN ('memory_extraction', 'steward_extraction', 'core_memory_refresh')
    RETURNING id, job_type, 'pending' AS orig_status
)
INSERT INTO outbox_migration_audit (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
SELECT :'batch_id', id, job_type, orig_status, 'deadletter',
       CASE
           WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete', 'memory_markdown_project', 'memory_cache_invalidate')
               THEN 'unsupported_handler'
           WHEN job_type IN ('memory_extraction', 'steward_extraction')
               THEN 'cancelled_legacy'
           ELSE 'quarantined_unknown'
       END
FROM affected;
```

关键变更：`job_type NOT IN ('memory_extraction', 'steward_extraction', 'core_memory_refresh')` 覆盖所有未知类型，包括未来可能出现的任何新 job_type。捕获所有剩余 pending，不留永久堆积。

#### 定期维护

```python
# 定期检查：是否有遗漏的 pending unknown Job
def quarantine_stale_unknown_jobs(db: Session, max_age_hours: int = 24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    db.query(OutboxJob).filter(
        OutboxJob.status == "pending",
        OutboxJob.job_type.notin_(ENABLED_OUTBOX_JOB_TYPES),
        OutboxJob.created_at < cutoff,
    ).update({
        OutboxJob.status: "deadletter",
        OutboxJob.terminal_reason: "quarantined_unknown",
        OutboxJob.error_message: "Quarantined: unknown job_type after maintenance window",
        OutboxJob.updated_at: datetime.now(timezone.utc),
    }, synchronize_session=False)
```

---

## H. 测试

### 新增测试矩阵

| # | 测试 | 断言 |
|---|------|------|
| **Handler 返回协议** | | |
| T1 | running + 有效其他 token → RETRY_LATER，OutboxJob 保持 pending | available_at 已设置，不会 completed |
| T2 | IngestionRun=succeeded → COMPLETED，Extractor 调用 0 次 | 不实例化 Extractor |
| T3 | source Turn 不存在 → NON_RETRYABLE，Extractor 调用 0 次 | deadletter + terminal_reason |
| T4 | TurnRecord.thread_id 不匹配 → NON_RETRYABLE，Extractor 调用 0 次 | deadletter |
| T5 | IngestionRun.deadletter → NON_RETRYABLE | OutboxJob 不会 completed |
| T6 | schema_version 不在 HandlerRegistry 支持的集合中 → Worker 直接 deadletter，Handler 不调用 | deadletter + unsupported_schema_version |
| T7 | schema_version 高于当前支持版本 → Handler 业务逻辑不执行 | Worker 层拒绝 |
| **双重 fencing** | | |
| T8 | Phase C: OutboxJob claim 已被新 Worker 接管、IngestionRun token 尚未更新 → 旧 Worker 写入被拒绝 | CLAIM_LOST |
| T9 | Phase C: OutboxJob lease 过期 → CLAIM_LOST | Outbox 状态不变 |
| T10 | Phase C: IngestionRun token 匹配但 OutboxJob claim 失效 → CLAIM_LOST | 不凭 IngestionRun 单独判断 |
| **CLAIM_LOST** | | |
| T11 | Phase C token 不匹配 → CLAIM_LOST，旧 Worker 不修改 Outbox | Outbox 状态不变 |
| T12 | Phase C run 已被接管 + succeeded → COMPLETED | 正常 finalize |
| T13 | Phase C run 已被接管 + deadletter → NON_RETRYABLE | Outbox deadletter |
| **原子 deadletter** | | |
| T14 | Outbox max_retries 耗尽 → OutboxJob + IngestionRun 在同一事务 deadletter | 同时提交或同时回滚 |
| T15 | deadletter 时旧 claim 已失效（fencing 失败）→ IngestionRun 不被修改 | rollback，Run 状态不变 |
| T16 | Extractor 连续失败耗尽重试 → Run 通过 ingestion_run_id 进入 deadletter | 不依赖 execution_token |
| **三阶段异常** | | |
| T17 | Phase B Extractor 抛异常 → Run=failed，execution_token=NULL | 下次 claim 可接管 |
| T18 | Phase B Extractor 抛异常 → RETRYABLE_ERROR(+run_id) | Outbox 重试，传播 ingestion_run_id |
| **租约绑定** | | |
| T19 | Heartbeat 期间 OutboxJob 被接管 → IngestionRun 不被重复接管 | Phase A 看到 busy → RETRY_LATER |
| T20 | Outbox claim 过期 → 新 Worker 可接管 IngestionRun | 新 execution_token |
| **WriteOutcome** | | |
| T21 | Gate reject 用 WriteOutcome.GATE_REJECTED → 不回滚其他 Proposal | 其他 Proposal 写入成功 |
| T22 | IGNORED / REINFORCE_SKIPPED → 不回滚批次 | 批次 succeeded |
| T23 | 不同 Gate reject reason 不破坏 no-op 判断 | 使用枚举而非字符串匹配 |
| **source Turn 验证** | | |
| T24 | Proposal.source_turn_id 与 TurnRecord.turn_id 一致 | 非空且匹配 |
| T25 | TurnRecord 不存在 → NonRetryableJobError | deadletter |
| **空结果** | | |
| T26 | Extractor 返回 [] → succeeded + proposal_count=0 | count=0 |
| T27 | proposal_count=0 重试 → Extractor 调用 0 次 | 直接 COMPLETED |
| T28 | 空 proposals 标记 succeeded 时 affected=0 → CLAIM_LOST | 不误标 completed |
| **迁移** | | |
| T29 | 历史 steward_extraction（无 source_turn_record_id）→ deadletter | cancelled_legacy |
| T30 | steward_extraction（有 source_turn_record_id）→ deadletter | cancelled_legacy，全量取消 |
| T31 | 合法的新 memory_extraction（有 source_turn_record_id）不被误杀 | 保留 pending |
| T32 | unknown pending → deadletter + terminal_reason | 不会永久 pending |
| T33 | Alembic upgrade 不使用 psql 元命令，可正常执行 | 无 \set 语法 |
| T34 | 同一 migration_batch_id + job_id 重复执行不产生重复审计行 | ON CONFLICT DO NOTHING |
| **Fencing / affected_rows** | | |
| T35 | retry_later affected=0 → FencingViolationError | 异常抛出，不修改 Outbox |
| T36 | finalize affected=0 → FencingViolationError | Worker 单独捕获，不 retry |
| T37 | finalize 抛 FencingViolationError 后不会再次 retry_or_deadletter | CLAIM_LOST 处理 |
| T38 | Registry remove 只移除指定 (job_id, claim_token) | 其他 claim_token 不受影响 |
| **统一 Handler 接口** | | |
| T39 | core_memory_refresh 使用 (ClaimedJob) -> HandlerResult 签名 | 返回 COMPLETED 或 RETRYABLE_ERROR |
| T40 | core_memory_refresh 自行管理 Session | commit/close 在 Handler 内部 |
| T41 | core_memory_refresh 走 worker_id + schema_version 校验 | HandlerRegistry 预校验 |
| **acquired/takeover** | | |
| T42 | pending Run → 决策为 acquired | decision=="acquired" |
| T43 | running Run + lease 过期 → 决策为 takeover | decision=="takeover" |
| **worker_id 传播** | | |
| T44 | ClaimedJob.worker_id 在 claim_one → Handler → Phase C 中一致 | 与 claim_one 写入值相同 |
| T45 | Heartbeat 续租使用 claimed.worker_id | 与 locked_by 一致 |
| **事务所有权** | | |
| T46 | Handler 返回后 Worker 不调用任何业务 commit | Worker 只操作 OutboxJob |
| T47 | Handler 自己管理 Phase A/C Session | 独立 commit/close |
| **批次死锁** | | |
| T48 | 相反顺序 Proposal 修改相同 keys → 不死锁 | 无 deadlock |
| **崩溃窗口** | | |
| T49 | 批次 commit 成功，Outbox finalize 前崩溃 → 重投不调 LLM | IngestionRun=succeeded |
| **Registry** | | |
| T50 | 同一 job_id 不同 claim_token → 互不覆盖 | 旧 claim lost，新 claim 独立 |
| **PostgreSQL 集成测试** | | |
| PG1 | FOR UPDATE SKIP LOCKED 并发 claim | 不同 claim_token |
| PG2 | ON CONFLICT DO NOTHING 并发 IngestionRun | 仅 1 条 |
| PG3 | SELECT FOR UPDATE 行锁互斥 | 第二个 Worker 等待 |
| PG4 | 旧 claim 在 deadletter 前失效 → IngestionRun 不被修改 | rollback，Run 状态不变 |
| PG5 | OutboxJob + IngestionRun deadletter 原子性 | 同时提交或同时回滚 |
| PG6 | Alembic upgrade 不包含 psql 元命令 | 正常执行 |
| PG7 | 同一 batch_id + job_id 不产生重复审计行 | ON CONFLICT DO NOTHING |

---

## 附录 A. 数据模型

### 修改：OutboxJob

```python
class OutboxJob(Base):
    __tablename__ = "outbox_jobs"

    # --- 现有列不变 ---

    # Phase 0.5B 新增
    locked_by: str | None              # String(64), nullable
    claim_token: str | None            # String(36), nullable
    lease_expires_at: datetime | None  # DateTime(tz=True)
    schema_version: int                # default=1
    available_at: datetime | None      # DateTime(tz=True)
    terminal_reason: str | None        # String(64), nullable
    original_status: str | None        # String(32), nullable  ← 仅迁移目标
    migration_batch_id: str | None     # String(36), nullable

    __table_args__ = (
        Index("ix_outbox_claim", "status", "available_at", "lease_expires_at"),
        Index("ix_outbox_migration_batch", "migration_batch_id"),
    )
```

### 新增：MemoryIngestionRun（无独立租约）

```python
class MemoryIngestionRun(Base):
    __tablename__ = "memory_ingestion_runs"

    id: str                           # PK, uuid4
    source_turn_record_id: str        # FK → turn_records.id
    extractor_name: str
    extractor_version: str
    status: str                       # pending | running | succeeded | failed | deadletter
    execution_token: str | None       # 复用 Outbox claim_token
    # 注意：无 lease_expires_at —— 活跃性通过 OutboxJob 判断
    started_at: datetime | None
    completed_at: datetime | None
    proposal_count: int               # default=0
    error_message: str | None
    created_at: datetime

    __table_args__ = (
        UniqueConstraint("source_turn_record_id", "extractor_name", "extractor_version"),
    )
```

### 修改：MemoryProposal (DB)

```python
class MemoryProposal(Base):
    __tablename__ = "memory_proposals"

    # --- 现有列不变 ---
    # Phase 0.5B 新增
    source_turn_id: str | None        # String(36), nullable  ← 通过验证确保非空
    ingestion_run_id: str | None      # String(36), nullable
    proposal_index: int | None        # Integer, nullable

    __table_args__ = (
        UniqueConstraint("ingestion_run_id", "proposal_index"),
    )
```

### 数据类

```python
@dataclass(frozen=True)
class ClaimedJob:
    id: str
    job_type: str
    payload: dict[str, Any]
    trace_id: str | None
    retry_count: int
    max_retries: int
    claim_token: str
    schema_version: int
    worker_id: str  # ← claim_one() 写入当前 Worker ID


@dataclass(frozen=True)
class ValidatedExtractionSource:
    turn_record_id: str
    thread_id: str
    turn_id: str


@dataclass(frozen=True)
class HandlerResult:
    outcome: HandlerOutcome
    reason: str = ""
    retry_available_at: datetime | None = None
    ingestion_run_id: str = ""
    terminal_reason: str = ""  # NON_RETRYABLE/deadletter 时写入 OutboxJob.terminal_reason


@dataclass
class IngestionRunResolution:
    decision: str  # already_succeeded | deadletter | busy | acquired | takeover
    run_id: str = ""
    lease_expires_at: datetime | None = None
    previous_status: str = ""
```

### Allowlist

```python
ENABLED_OUTBOX_JOB_TYPES: frozenset[str] = frozenset({
    "memory_extraction",
    "core_memory_refresh",
})
```

---

## 附录 B. 迁移

### 审计表

```sql
CREATE TABLE IF NOT EXISTS outbox_migration_audit (
    id SERIAL PRIMARY KEY,
    migration_batch_id VARCHAR(36) NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    job_type VARCHAR(64),
    original_status VARCHAR(32),
    new_status VARCHAR(32),
    terminal_reason VARCHAR(64),
    migrated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(migration_batch_id, job_id)
);
```

### 迁移 SQL（通过 Alembic Python 迁移执行）

禁止在 Alembic 迁移脚本中使用 `\set` 等 psql 元命令。`migration_batch_id` 在 Python 代码中定义为常量，通过参数绑定传入。

```python
# alembic/versions/xxxx_phase05b.py

MIGRATION_BATCH_ID = "phase05b_migration_v6_001"

def upgrade():
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS outbox_migration_audit (
            id SERIAL PRIMARY KEY,
            migration_batch_id VARCHAR(36) NOT NULL,
            job_id VARCHAR(36) NOT NULL,
            job_type VARCHAR(64),
            original_status VARCHAR(32),
            new_status VARCHAR(32),
            terminal_reason VARCHAR(64),
            migrated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(migration_batch_id, job_id)
        )
    """))

    # Step 1: 取消崩溃残留 running
    op.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: running job without active worker',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'running'
            RETURNING id, job_type, 'running' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 2: 取消历史 extraction（缺少 source_turn_record_id）
    op.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: historical extraction (missing source_turn_record_id)',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type IN ('memory_extraction', 'steward_extraction')
              AND (payload->>'source_turn_record_id' IS NULL
                   OR payload->>'source_turn_record_id' = '')
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 3: 取消所有剩余 pending steward_extraction
    op.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = 'cancelled_legacy',
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'cancelled: steward_extraction not in allowlist',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type = 'steward_extraction'
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter', 'cancelled_legacy'
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 4: 隔离所有不在 allowlist 中的 pending Job
    op.execute(sa.text("""
        WITH affected AS (
            UPDATE outbox_jobs
            SET status = 'deadletter',
                terminal_reason = CASE
                    WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete',
                                      'memory_markdown_project', 'memory_cache_invalidate')
                        THEN 'unsupported_handler'
                    ELSE 'quarantined_unknown'
                END,
                migration_batch_id = :batch_id,
                original_status = status,
                error_message = 'quarantined: job_type not in allowlist',
                locked_by = NULL, claim_token = NULL, lease_expires_at = NULL,
                updated_at = NOW()
            WHERE status = 'pending'
              AND job_type NOT IN ('memory_extraction', 'core_memory_refresh')
            RETURNING id, job_type, 'pending' AS orig_status
        )
        INSERT INTO outbox_migration_audit
            (migration_batch_id, job_id, job_type, original_status, new_status, terminal_reason)
        SELECT :batch_id, id, job_type, orig_status, 'deadletter',
               CASE
                   WHEN job_type IN ('memory_vector_upsert', 'memory_vector_delete',
                                     'memory_markdown_project', 'memory_cache_invalidate')
                       THEN 'unsupported_handler'
                   ELSE 'quarantined_unknown'
               END
        FROM affected
        ON CONFLICT (migration_batch_id, job_id) DO NOTHING
    """), {"batch_id": MIGRATION_BATCH_ID})

    # Step 5: 设置默认值
    op.execute(sa.text("""
        UPDATE outbox_jobs
        SET schema_version = COALESCE(schema_version, 1),
            available_at = COALESCE(available_at, created_at)
        WHERE schema_version IS NULL OR available_at IS NULL
    """))


def downgrade():
    # 1. 根据 audit 恢复 Job
    op.execute(sa.text("""
        UPDATE outbox_jobs o
        SET status = a.original_status,
            terminal_reason = NULL,
            migration_batch_id = NULL,
            original_status = NULL,
            error_message = NULL,
            updated_at = NOW()
        FROM outbox_migration_audit a
        WHERE o.id = a.job_id
          AND a.migration_batch_id = :batch_id
    """), {"batch_id": MIGRATION_BATCH_ID})

    # 2. 删除该批次 audit
    op.execute(sa.text("""
        DELETE FROM outbox_migration_audit
        WHERE migration_batch_id = :batch_id
    """), {"batch_id": MIGRATION_BATCH_ID})

    # 3. 删除 MemoryIngestionRun 表
    op.drop_table("memory_ingestion_runs")

    # 4. 删除 OutboxJob 新增列
    op.drop_column("outbox_jobs", "migration_batch_id")
    op.drop_column("outbox_jobs", "original_status")
    op.drop_column("outbox_jobs", "terminal_reason")
    op.drop_column("outbox_jobs", "available_at")
    op.drop_column("outbox_jobs", "schema_version")
    op.drop_column("outbox_jobs", "lease_expires_at")
    op.drop_column("outbox_jobs", "claim_token")
    op.drop_column("outbox_jobs", "locked_by")

    # 5. 删除 MemoryProposal 新增列和约束
    op.drop_constraint("uq_ingestion_run_proposal_index", "memory_proposals")
    op.drop_column("memory_proposals", "proposal_index")
    op.drop_column("memory_proposals", "ingestion_run_id")
    op.drop_column("memory_proposals", "source_turn_id")

    # 6. 最后删除审计表
    op.drop_table("outbox_migration_audit")
```

---

## 附录 C. 精确修改文件

| # | 文件 | 变更 |
|---|------|------|
| 1 | `db/models.py` | OutboxJob +8 列+2 索引；MemoryIngestionRun 新表（无 lease_expires_at）；MemoryProposal +3 列+1 约束 |
| 2 | `worker/outbox_worker.py` | 重写：单例模式、claim_one(DTO, worker_id)、_deadletter_job_and_ingestion_run(原子)、finalize/retry/retry_later(fencing)、poll(基于 HandlerResult + schema 预校验)、allowlist、enqueue 拒绝 unknown |
| 3 | `worker/outbox_heartbeat.py` | **新建**：ActiveClaimRegistry (job_id+claim_token 键) + OutboxHeartbeat |
| 4 | `worker/outbox_handlers.py` | 重写：handle_memory_extraction + handle_core_memory_refresh 均接收 ClaimedJob → 返回 HandlerResult；三阶段自管 Session；Phase C 双重 fencing |
| 5 | `worker/outbox_dto.py` | **新建**：ClaimedJob(+worker_id), ActiveClaim, HandlerResult(+terminal_reason), HandlerOutcome, ValidatedExtractionSource, IngestionRunResolution |
| 6 | `worker/handler_registry.py` | **新建**：HandlerRegistry（含 supported_schema_versions 预校验） |
| 7 | `main.py` | lifespan 中创建单例 Worker/Heartbeat/HandlerRegistry；停止时 wait_active |
| 8 | `memory/memory_types.py` | WriteResult + WriteOutcome；MemoryProposal +3 字段 |
| 9 | `memory/memory_write_service.py` | write_batch() + _write_single_in_transaction()；WriteOutcome 枚举判断；批量锁排序；_enqueue_projection 增加 capability flag |
| 10 | `memory/recall_config.py` | ProjectionCapabilities 配置 + ENABLED_OUTBOX_JOB_TYPES |
| 11 | `alembic/versions/xxxx_phase05b.py` | Python 迁移（不含 psql 元命令）+ 审计表（UNIQUE）+ upgrade/downgrade 完整流程 |
| 12 | `tests/integration/test_outbox_pg.py` | **新建**：PostgreSQL 集成测试（含原子 deadletter、审计唯一性） |
| 13 | `tests/unit/backend/test_outbox_fencing.py` | **新建**：Fencing/Registry/HandlerResult/worker_id 测试 |
| 14 | `tests/unit/backend/test_memory_ingestion.py` | **新建**：IngestionRun 三阶段 + source Turn 验证测试 |

---

## 附录 D. 回滚

```bash
alembic downgrade -1
psql -c "UPDATE outbox_jobs o SET status = a.original_status, ... FROM outbox_migration_audit a WHERE o.id = a.job_id AND a.migration_batch_id = 'phase05b_migration_v6_001'"
psql -c "DELETE FROM outbox_migration_audit WHERE migration_batch_id = 'phase05b_migration_v6_001'"
git revert <phase05b-commit>
```

---

## 已知限制

1. **vector/FTS/KG 投影**：capability flag 防止 enqueue，不假装成功。
2. **Legacy extraction**：缺少 `source_turn_record_id` 的旧 Job 被取消。
3. **SQLite**：`FOR UPDATE SKIP LOCKED` 和 `ON CONFLICT` 需 PostgreSQL。集成测试用 PG。
4. **core_memory_refresh** 已验证幂等（确定性重建 + stale-version 保护 + checksum）。✅
5. **IngestionRun 活跃性**通过 OutboxJob 判断，不维护独立 lease。这简化了两个租约漂移的问题。
