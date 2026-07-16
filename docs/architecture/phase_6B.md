# AIive Phase 6B：数据保留治理、generation 清理、真空回收与长期压力/混沌测试

> 本文件为 **Phase 6B 实施方案**（拆分自 `phase_6.md`，依赖 `phase_6A.md` 的 Forget Saga / Shield / Verifier）。
> 范围：数据保留治理 + 派生投影 generation 清理 + 数据库物理空间回收 + 长期压力与混沌测试 + 6B 测试矩阵。
> **本次不修改任何代码、不生成 migration、不执行 purge、不创建测试数据。**
> 所有保留/清理策略须遵守核心原则：**用户源数据不因普通保留策略物理删除；只有明确 forget 才删用户内容。**

---

## 0. Phase 6B 在 Phase 6 中的定位

Phase 6A 解决“忘得对、忘得彻底、可验证”。Phase 6B 收口**长期运行中的派生数据增长与物理空间回收**，并构建**可重复的长期模拟**验证 Saga 在规模/时间/崩溃下的不变量。

Phase 6B 不新增 Forget Saga 语义，但消费 6A 的产物：
- `forget_operations` / `forget_batches` / `forget_actions` 的终态记录用于 retention 清理；
- `ForgetTombstone` 的最小审计行长期保留；
- `content_provenance_refs`（§8）用于按源定位 legacy 副本做 coarse purge。

---

## Q. Retention 清理策略（派生数据增长治理）

### Q.0 原则
- **用户源数据不因普通保留策略物理删除**（`events`/`turn_records`/`memory_records` 等 SOT 行，除非明确 forget 且走 6A Phase F 的不可逆 scrub/安全删）；
- 派生投影旧版本可按保留期清理；
- 已完成 operational job 可归档/清除；deadletter 保留更久；
- 最小 `ForgetTombstone` 不含内容，可长期保留；
- **所有清理任务有界、可续跑、可审计**；
- **不允许每日全表无界扫描**（使用续跑游标，复用 P4 lane 模式）。

### Q.1 复用现有结构
- `RetrievalIndexGeneration` 的 `status`：`building`/`active`/`retired`/`failed`——`retired` generation 的 `RetrievalIndexEntry`/`Token` 可清理（不影响 active generation）。
- 复用现有 Maintenance / Outbox / P4 续跑模式，**先审计现状，能复用则复用，不强行加无必要新表**。

### Q.2 新增表（仅在现有模式不足以表达时）
若 `RetrievalIndexGeneration`/`OutboxJob` 的 `status` + `completed_at` 不足以表达保留策略，才新增：
```text
retention_policies
  id PK
  target_type   String(32)   # outbox_job|memory_maintenance_input|retrieval_generation|segment_summary|epoch_checkpoint|compaction_input|forget_action|log
  retention_days Integer
  scope         String(32) nullable
  enabled       Boolean
  created_at / updated_at

retention_cleanup_runs
  id PK
  policy_id     FK nullable
  target_type   String(32)
  stage         String(16)   # scan|delete|archive|done|failed
  cursor        String(64)   # 续跑游标
  input_hash    String(64)
  scanned_count Integer
  deleted_count Integer
  status        String(16)
  created_at / updated_at
```
默认优先复用 `OutboxJob`/`RetrievalIndexGeneration` 既有字段，避免新增表；仅在审计证明不足时引入上述两张。

### Q.3 清理对象与保留期（建议默认，可配置）
| 对象 | 触发 | 保留期（默认） | 动作 |
|----|----|----|----|
| 已完成 `OutboxJob` | `status='completed'` 且 `completed_at` 超期 | 30 天 | 删除 payload/error_message 大字段，保留最小审计行或归档 |
| `deadletter` OutboxJob | `status='deadletter'` | 180 天（更长） | 保留更久，人工复盘后清 |
| 旧 `MemoryMaintenanceInput`/`Action` | Run 完成超期 | 90 天 | 清除（不含用户源数据） |
| 旧 `RetrievalIndexGeneration`（retired） | `status='retired'` 超期 | 30 天 | 清除其 Entry/Token |
| 旧 `SegmentSummary` version | 非当前 `Segment.summary_id` 超期 | 60 天 | 清除旧版本内容 |
| 旧 `EpochCheckpoint` version | 非当前 `Epoch.checkpoint_id` 超期 | 60 天 | 清除旧版本内容 |
| 旧 `CompactionInput` / `EpochCompactionInput` | 超期 | 90 天 | 清除（`working_state_snapshot` 含内容，优先） |
| `ForgetAction` | `forget_operations` 已 `purged` 超期 | 90 天 | 清除（审计） |
| 运行日志 / 临时 error payload | 超期 | 30 天 | 清除 |
| `ForgetTombstone` | 长期 | 不自动清 | 最小审计，可长期保留 |
| `content_provenance_refs` | 关联副本已清 | 同副本 | 清除 |

### Q.4 有界续跑
- 每轮清理以 `batch_size`（如 500 行）+ `cursor`（按 `id`/时间）推进，正常分页 `CONTINUE`，不成长事务；
- `retention_cleanup_runs` 记录 `cursor`/`scanned_count`/`deleted_count`，崩溃后从 `cursor` 续跑；
- reconciler 补发遗漏的清理 Job。

---

## R. 数据库物理空间回收

删除数据库行 ≠ 文件立即缩小。必须显式维护，且**不在用户请求事务内执行**。

### R.1 PostgreSQL
- 依赖 `autovacuum`；定期 `VACUUM ANALYZE` 由独立低优先级维护任务执行（不在请求路径）；
- `VACUUM FULL` 锁表，**仅在低峰/维护窗口**考虑，禁止在请求路径执行；
- 索引膨胀用 `REINDEX`（可 `CONCURRENTLY`）；
- retrieval token 大量删除后建议定期 `VACUUM` + `REINDEX` 相关索引。

### R.2 SQLite
- WAL checkpoint：`PRAGMA wal_checkpoint(TRUNCATE)`；
- `VACUUM` 阻塞性强，**仅在独立维护任务、低峰执行**；大量 retrieval token 删除后建议定期 `VACUUM`；
- 不在用户 forget 请求事务内执行 `VACUUM`。

### R.3 触发策略
- 空间回收由**独立低优先级维护任务**执行（复用 P4 scheduler lane），不阻塞请求路径；
- 可基于表增长阈值（如 `events`/`retrieval_index_tokens` 删除量超阈值）触发，而非固定每日全表。

---

## W. 长期压力与混沌测试

### W.1 数据规模（测试工具须支持，按资源分级）
- 1k / 10k / 100k `MemoryRecord`；100k `TurnRecord`；1M `Event`；多 Segment/Epoch；多 retrieval generation；大量 sleeping/archived/forgotten。

### W.2 时间模拟
- 30 / 90 / 365 天，行为含：新对话、memory extraction、reinforce、supersede、sleep/wake、segment sealing、epoch rollover、retrieval refresh、index rebuild、forget memory / history / everywhere、retention cleanup、进程重启。

### W.3 混沌点（强制 crash，验证接管）
在以下阶段强制 crash，验证接管后：
- ForgetOperation 创建后 / shield 完成后 / 部分 Action 完成后 / Summary 重建前后 / Checkpoint 重建前后 / 索引 tombstone 前后 / 内容 scrub 后 / 物理 delete 前后 / verification 前后。
验证：
- 不重复删错误目标；
- 不重新暴露 shielded 内容；
- 不产生半条 Summary；
- 不泄漏旧 Checkpoint；
- 不丢未删数据；
- 不出现永久 running Job；
- CONTINUE 不增失败。

### W.4 性能指标基线（不要求绝对 SLA，建基线 + 回归阈值）
ContextAssembler p50/p95、auto retrieval p50/p95、search/deep p50/p95、Memory write p50/p95、Daily Dream p50/p95、Forget shield 延迟、Forget cascade 完成批次数、索引增长、数据库增长、Outbox backlog、每轮 token 使用。

### W.5 必须验证的不变量（长期）
- 同一 ForgetOperation 只有一份 Selector Manifest + 冻结 Target/Dependency；
- 未在 Manifest 中的记录绝不删除；
- forgotten 内容任何读取模式均不可返回（Shield + Tombstone 双保险）；
- archived 仍可按 P5 规则显式读取；
- forget 优先级高于 pinned/user_required；
- 旧 source Event 不会重新生成已忘记 Memory；
- 新用户输入可以重新建立新事实；
- Summary/Checkpoint 不包含已删除来源；
- Retrieval token 不包含 forgotten 内容；
- Core Memory 不包含 forgotten 内容；
- 物理 purge 后不存在内容副本（或仅含不可恢复 scrub 残骸）；
- 最小 Tombstone 不包含原文；
- 正常重试不会重复执行副作用；
- 旧 Worker 无法提交新 claim 的结果；
- 上下文 token 仍严格有界；
- 长期维护工作量与增量相关（retention 有界续跑，无全表扫描）；
- retention 清理不删除用户源数据（除非 6A forget 已 scrub）。

---

## V. Phase 6B 测试矩阵（覆盖 spec 第 19 节相关项 + 规模/混沌）

1. retention 清理**不删除**任何用户源数据（events/turn_records/memory_records）行；
2. 派生投影旧版本（SegmentSummary/EpochCheckpoint/CompactionInput）按保留期清理，不影响当前版本；
3. `retired` RetrievalIndexGeneration 的 Entry/Token 清理不影响 `active` generation；
4. 已完成 OutboxJob 按保留期归档/清除，deadletter 保留更久；
5. `ForgetAction` 在 Operation purged 超期后清除，但 `ForgetTombstone` 保留；
6. retention 清理任务有界续跑（cursor 推进，崩溃不重复删）；
7. reconciler 补发遗漏的 retention Job；
8. PostgreSQL `VACUUM ANALYZE` / `REINDEX` 由独立任务执行，不在请求路径；
9. SQLite `VACUUM` 仅在低峰独立任务执行；
10. retrieval token 大量删除后索引膨胀可控（`REINDEX` 生效）；
11. 30/90/365 天模拟不变量全通过（W.5）；
12. 100k Memory / 1M Event 压力测试下 ContextAssembler / auto retrieval p50/p95 不显著退化；
13. 任意阶段 crash 后不泄漏 forgotten 内容（接管正确）；
14. 长期运行 Outbox backlog 有界（retention + forget 续跑不留永久 running）；
15. 数据库增长可测量、可控制（retention 清理生效）；
16. 每轮 token 使用不随 forget/retention 增长（上下文持续有界）；
17. 不存在无界全表删除或扫描（所有清理走 cursor/batch）；
18. 无 Supervisor LLM、无关键词硬编码路由（回归）；
19. P0.5A～P5 全量回归（6B 不引入回归）；
20. 6A + 6B 端到端：forget everywhere → verifier → purge → retention 清理 → 空间回收，全链路不变量成立。

---

## X. Phase 6B 回滚与降级

- **Retention 误删防护**：所有清理为幂等 + 有界 + 可审计；误删仅影响派生旧版本（可重建），不影响用户源数据。
- **Vacuum 失败**：独立任务失败不影响请求路径，下次重试；`VACUUM FULL` 仅在维护窗口，失败可回退到 `VACUUM ANALYZE`。
- **混沌测试失败**：定位 Saga 不变量缺口，回 Phase 6A 修复（如 fencing / batch 续跑 / Tombstone 拦截），不在 6B 内打补丁绕过。
- **降级**：retention 任务可暂停（配置开关），暂停时仅停止清理，不回退已 forget 的屏蔽状态。

---

## 附录：Phase 6B 依赖的 6A 产物

- `forget_operations` / `forget_batches` / `forget_actions`（终态用于 retention）
- `forget_tombstones`（最小审计，长期保留）
- `content_provenance_refs`（legacy 副本按源定位）
- `ForgetVisibilityService`（读取路径 fail-closed，6B 不改动其屏蔽语义）

## 下一步

Phase 6A 先实施并验收；随后 Phase 6B 在 6A 验收基础上实施 retention / vacuum / 长期混沌测试。
