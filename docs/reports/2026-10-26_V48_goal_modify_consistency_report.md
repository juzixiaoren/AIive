# V48 长期目标修改一致性与审批有效性 — 交付报告

日期：2026-10-26
范围：`backend/aiive/task_runtime/goal_service.py` + 离线验收测试
约束：不新增任务引擎、不新增表、不新增迁移；保留 V47 未提交改动。

## 1. 发现并修复的真实缺口

| # | 缺口 | 后果 | 修复 |
|---|---|---|---|
| 1 | `modify` 只比较 `schedule`，其余字段变化也按“步骤未变”处理 | 改了任务说明/验收标准/依赖/执行模式后，`done` 仍是 `done`、`evidence_refs` 与 `approved_at` 原样保留：旧结论与旧审批继续生效 | 引入 `CONTENT_FIELDS = (title, kind, acceptance, depends_on, requires_evidence)`；内容变化即 `_invalidate_step`（状态回 pending、清空证据、清空审批、清触发、清 last_result/note=content_changed） |
| 2 | 下游步骤不随依赖重做而失效 | 依赖被重做的步骤已重排，依赖它的步骤仍显示 `done`，目标可能被误判完成 | 按依赖图递归到不动点，失效原因记 `dependency_changed:<dep>`；与失效链无关的步骤保持原状态 |
| 3 | `approve_step` / `start_step` / `on_run_started` 不校验依赖 | 审批（或用户开始）可以把依赖未满足的步骤直接 `ready` 并起跑，绕过计划顺序 | 三处都加 `dependencies_met` 校验：API 抛 `step_dependencies_unmet`，运行时兜底回退到 `next_runnable_step` |
| 4 | 运行中修改计划会让步骤结算与计划错位 | 步骤被重置但 run 仍在跑，`on_step_finished` 拿到 `current_step=None`，可能跳过被重置的步骤直接完成 | `modify` 在 `dispatching/running/verifying/reconciling` 时拒绝（`goal_running`），要求先暂停 |

另：`approve_step` 补写 `Checkpoint(reason="goal_step_approved")`，使审批与确认/修改一样可审计、可回放；
审批本身仍落在 `GoalStep.approved_at`（task_state JSON），**没有**强行复用要求 `action_id` 的 `ApprovalRequest`。

## 2. 修改语义（新）

```
执行内容字段：title / kind / acceptance / depends_on / requires_evidence
  任一变化 → 该步骤失效（pending + 清证据 + 清审批 + 清触发 + note=content_changed）
           → 依赖它的下游递归失效（note=dependency_changed:<dep>）
调度字段：schedule（kind/at/weekday/time/timezone/until）
  只改时间 → 已完成步骤保持 done 与 evidence；未完成步骤重置 pending 并重新排期
未列出的步骤（含新增步骤）→ 新增 pending；其余原样保留状态与证据
```

审计：`goal_modified` 事件现在带 `added / rescheduled / invalidated[{step_id,reason}] / approval_invalidated`，
可直接回答“这次修改让哪些步骤重做、哪些审批作废”。

## 3. 审批有效性

- 执行内容变化 → `approved_at` 置空，步骤回到 `waiting_user` 并重新发 `goal_step_approval` 通知；
  未重新审批前：不新建 run，排队中的 run 也会因 `on_run_started` 无可用步骤而停在 `blocked_user`。
- 重复审批：第二次 `approve_step` 抛 `step_already_approved`，`approved_at` 与 outbox job 数量都不变（不会重复起跑）。
- 依赖未满足：审批直接拒绝（`step_dependencies_unmet`），不会把顺序绕过去。
- 仅改调度时间：审批保留（审批针对的是执行内容，不是时间）。
- 权限校验现状：`/api/agent-tasks/*` 与确认/结束等控制面一样，没有路由级 token 依赖，
  权限边界来自部署层（与既有任务控制面一致），本轮未改变；`approve_step` 只做业务校验
  （goal active / kind / 未完成 / 未审批 / 依赖已满足）。

## 4. 测试

新增 7 例（`tests/unit/backend/test_long_term_goal.py`，全离线 Fake LLM）：

| 用例 | 覆盖 |
|---|---|
| `test_modify_content_change_invalidates_done_step_and_evidence` | 内容修改但时间不变：done → ready、证据清空、审计事件正确 |
| `test_modify_schedule_only_keeps_completed_step` | 仅改时间：已完成步骤保留完成状态与证据，未完成步骤重新排期 |
| `test_modify_dependency_change_invalidates_downstream` | 依赖变化/上游重做：下游递归失效，无关步骤不受影响 |
| `test_modify_content_change_invalidates_approval` | 旧审批失效：approved_at 清空、重新等待审批、排队 run 不越权执行 |
| `test_repeated_approval_keeps_single_run` | 重复审批：拒绝且不重复入队 |
| `test_approve_requires_dependencies_met` | 审批不得绕过依赖 |
| `test_modify_rejected_while_step_is_running` | 运行中拒绝修改，计划版本不变 |

命令与结果：

```
.venv/Scripts/python.exe -m pytest tests/unit/backend/test_long_term_goal.py -q      # 27 passed
.venv/Scripts/python.exe -m pytest tests -q                                        # 1275 passed, 23 skipped
.venv/Scripts/python.exe -m ruff check --select F,E9 backend/aiive/task_runtime/ ...  # All checks passed
/e/python/Scripts/basedpyright backend/aiive                                        # 0 errors / 0 warnings / 0 notes
```

真实 PostgreSQL 校验（本轮**无表结构变更、无迁移**，仍做了两项实证）：

- `alembic check`（连本机 pgvector/pg16 容器）：`No new upgrade operations detected.`
- 事务内行为校验（同一 PG，最后 `rollback`，不留数据）：`attach_plan → confirm → approve_step`
  后从库里重读 `approved_at` 存在、`goal_step_approved` checkpoint 恰好 1 条；
  `modify` 内容变化后重读为 `approved_at is None`、`evidence_refs == []`、
  `note=content_changed`、下游 `dependency_changed:migrate`，输出 `PG_CHECK_OK 3 waiting_user pending`。

## 5. 遗留风险

1. **修改只做“失效”，不做“自动重排下游内容”**：下游步骤被重置为 pending 后按原依赖顺序重跑，
   不会因为上游验收标准变化而自动改写下游步骤的验收标准——这需要 LLM 重新规划，属于提案侧职责。
2. **运行中禁止修改**：用户在步骤执行中改计划会收到 `goal_running`，需要先暂停；
   这是刻意取舍（否则结算与计划会错位），但 UI 目前只显示错误文本，没有“暂停并修改”的一键路径。
3. **证据行不删除**：失效只清 `GoalStep.evidence_refs`，`task_evidence` 行保留（内容寻址、可被其他步骤或审计引用）；
   因此“证据失效”是引用层面的失效，不是物理删除。
4. **内容变化的判定是字段级精确比较**：改标点、空白也算内容变化，会触发重做；
   反过来，只改 `schedule` 之外的元数据（如 `note`）不算变化，符合预期。
5. **审批仍是步骤级**：`approved_at` 不是 `ApprovalRequest`，审批列表页看不到目标步骤审批，
   只能从任务中心/目标 API 处理（V47 已记录，本轮未改变）。
