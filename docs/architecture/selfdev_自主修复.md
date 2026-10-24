# 自主修复闭环

> 范围：从"明确的修复目标"到"候选版本通过测试与安全门"的完整闭环。
> 晋升后的进程切换、健康检查、观察窗与失败回滚仍由现有 Supervisor / ReleaseManager 负责。
> 不涉及 Model Router、Reranker、Temporal Graph。

## 1. 为什么需要闭环

现有自进化能力是**分段**的：

- `selfdev/planner.py` 只接收一段目标文本，不知道仓库里真实的文件内容；
- `selfdev/patch_executor.py` 能把补丁写进非活跃槽位，但不会判断补丁是否真的修好了问题；
- `selfdev/targeted_test_runner.py` 能跑定向测试，但测试失败后没有"根据错误继续修"的环节；
- `eval/gate.py` 与 `supervisor/release_manager.py` 已经能阻断不合格候选，但需要人把前面的步骤串起来。

缺的不是新组件，而是**编排 + 真实反馈 + 有界重试 + 可追溯记录**。本轮只补这一层。

## 2. 执行链

```
目标 goal
  │
  ├─ 0. 取锁 + 恢复    RepairLock.acquire() → recover_interrupted()
  │                   同一项目同一时间只允许一个修复流程；
  │                   按 journal 回滚上次中断的运行（绝不标记为成功）
  │
  ├─ 1. 代码理解      ProjectReader.collect_context(goal)
  │                   只读检索 + 读取相关源码/相关测试
  │
  ├─ 2. 规划          SelfDevPlanner.plan(goal, context=...)
  │                   复用 selfdev.patch_plan 提示词；逐条校验操作
  │
  ├─ 3. 应用补丁      CandidateSlotGuard.mark_not_promotable() → PatchExecutor.apply_to_inactive(operations)
  │                   记录活跃槽位基线指纹 → 标记候选不可晋升 → 只写非活跃槽位
  │
  ├─ 4. 定向测试      TargetedTestRunner(candidate_root).run_for_changed_files(...)
  │
  ├─ 5. 有限修复      测试失败 → 真实错误回灌 → 重新规划 → 重新应用 → 重新测试
  │                   最多 max_rounds 轮；补丁指纹相同即停止
  │
  ├─ 6. 安全门        evaluate_promotion_gate(slot_root)
  │                   内核契约 + Replay Eval + 候选子进程评测（fail-closed）
  │
  └─ 7. 受控晋升      ReleaseManager.promote()（可选）
                      健康检查 → 安全门 → Supervisor 切换 → 观察窗/回滚
```

实现入口：`backend/aiive/selfdev/repair_loop.py::SelfDevRepairLoop.run`。

## 3. 代码理解：为什么新增 ProjectReader

要求是"复用现有文件读取、搜索和本机 Shell 能力，只有确实不足才补 Project Reader"。核查结论：

| 现有能力 | 是否可复用 | 原因 |
|---|---|---|
| `read_text_file` | 否 | 硬限定在 `~/Documents`，无法读取仓库源码 |
| `run_shell` | 不适合 | Task 工具，受 TaskScope/审批约束；服务端自进化流水线没有 Task 上下文 |
| `read_document` | 否 | 面向用户文档的多格式提取，不是仓库代码检索 |

因此新增 `selfdev/project_reader.py`，只做**仓库范围内的只读、有界**读取：

- `search(keywords)`：大小写不敏感子串检索，返回 `path:line:text`；
- `read(relative_path)`：单文件读取，默认上限 40KB；
- `collect_context(goal, explicit_files=...)`：提取目标中的文件路径/标识符 → 检索 → 取命中最多
  的若干文件 → 附带同名测试文件（`tests/**/test_<stem>.py`）；
- `format_context_for_prompt`：渲染成提示词片段，总量上限 120KB。

它**不提供写能力**。所有修改仍然只能走 `PatchExecutor`。

### 安全边界

- 拒绝绝对路径、`..` 穿越、符号链接逃逸（`Path.resolve()` + `is_relative_to`）；
- 拒绝 `.env`（保留 `.env.example`）、`*.pem/*.key/*.db/*.log`、`id_rsa*`、`credentials.json`；
- 跳过 `.git/.venv/node_modules/__pycache__/data/runtime/slots/.data/postgres/qdrant/object_store/logs`；
- 单文件 40KB、单次扫描 20k 文件 / 1MB 单文件、上下文总量 120KB。

## 4. 补丁生成与隔离

`SelfDevPlanner.plan` 新增三个关键字参数，但仍复用**同一个** `selfdev.patch_plan` 提示词
（不新增提示词条目，避免破坏提示词目录契约）：

- `context`：ProjectReader 的只读代码上下文；
- `previous_plan`：上一轮已应用但测试失败的计划；
- `failure_feedback`：上一轮测试失败的真实输出。

三者被拼进 `goal` 变量，作为"待规划数据"。模型仍然必须输出完整最终文件内容；
`_validate` 继续阻断未知操作、缺失 content、Trusted Core 路径。

补丁应用保持原有隔离语义：

- 每次 apply 都从**活跃槽位**重新同步基线，结果确定且幂等；
- 只写非活跃槽位的 `app/`；
- 路径必须落在 `app/` 内；
- 删除只走 `safe_delete`（scope 限定在非活跃槽位 app）；
- Trusted Core 路径（`db/`、`control/`、`task_runtime/`、`supervisor/`、`api/routes_selfdev.py` 等）
  在规划与执行两层都被拒绝。

## 5. 有限修复循环

`SelfDevRepairLoop.run` 的停止条件全部显式：

| 条件 | 状态 | 说明 |
|---|---|---|
| 规划器异常 / 返回非法结构 | `planner_failed` | 不重试，不伪造计划 |
| 没有任何可执行操作 | `blocked` | 全部 `not_allowed_yet` 时不应用补丁 |
| 补丁应用失败（含部分失败） | `apply_failed` | 不测试半成品，记录失败 reason |
| 测试通过 + 安全门通过 + 未请求晋升 | `tests_passed` | 候选就绪，等待人工/后续晋升 |
| 测试通过 + 安全门通过 + 请求晋升 | `promote_requested` / `promoted` | 由 Supervisor 决定是否切换 |
| 测试通过 + 安全门失败 | `gate_failed` | 绝不晋升 |
| 测试持续失败且轮次耗尽 | `tests_failed` + `max_rounds_exhausted` | `ok=False` |
| 模型提交完全相同的补丁 | `tests_failed` + `repeated_patch_fingerprint` | 提前停止，避免空转 |

`max_rounds` 默认 3，硬上限 5；总尝试次数 = `max_rounds + 1`（首次规划 + 修复轮）。

**补丁指纹** = `sha256(sorted(operation, target_file, sha256(content), not_allowed_yet))`。
指纹相同意味着继续重试必然得到相同结果，因此直接停止。

## 6. 并发控制、候选隔离与中断恢复

`selfdev/repair_guard.py` 只补三件缺失的事，不重复实现版本管理与晋升。

### 6.1 并发锁 `RepairLock`

- 锁文件 `runtime/selfdev/locks/repair-<key>.lock`，由 `O_CREAT|O_EXCL` 原子创建，内容带 owner token；
- 只有持有者能释放（token 不符时绝不删除别人的锁）；
- 持有进程仍存活时绝不抢占；持有进程已退出时可安全接管；
- 取锁失败返回 `repair_busy`，不排队、不重试。

### 6.2 候选隔离 `CandidateSlotGuard`

- 每轮修复先 `capture_baseline`：记录活跃槽位版本 + `tree_fingerprint`；
- 改写候选代码**之前** `mark_not_promotable`，写 `slots/<X>/candidate_state.json`；
- 失败后 `restore` 整树回滚到基线并复核指纹；恢复不了就保持"不可晋升"标记，避免失败代码被误晋升；
- 晋升前 `ReleaseManager._candidate_block` 核对不可晋升标记与指纹，不符即阻断。

### 6.3 中断恢复 `RepairJournal`

- 每个阶段落 `runtime/selfdev/runs/<run_id>.json` 阶段快照（随槽位 runtime 目录；无基线时不写）；
- 终态记录保留 7 天后由 `prune()` 清理；
- `recover_interrupted`：跳过持有者仍存活的运行，按日志回滚候选槽位并隔离，状态只写
  `interrupted*`（`interrupted` / `interrupted_superseded` / `interrupted_promotion_pending` /
  `interrupted_recovered` / `interrupted_quarantined`），绝不把不完整结果标记为成功；
- 指纹一律由 `slot_manager.tree_fingerprint` 计算，`None` 表示"无法验证"，调用方必须按失败处理。

## 7. 安全门与受控晋升

- 测试通过后，`run_gate=True`（默认）调用 `evaluate_promotion_gate(slot_root)`：
  内核契约 + Replay Eval + 候选子进程评测，任一失败/异常一律阻断；
- `promote=True` 时才调用 `ReleaseManager.promote()`。该入口**自己会再跑一次安全门**
  （生产路径的权威闸门），并负责健康检查、Supervisor 切换、观察窗与自动回滚；
- 若 Supervisor 返回 `switch_requested`，记录为 `promote_requested`，**不声称已晋升**；
- 回滚仍走 `ReleaseManager.rollback()`，是紧急逃生通道，刻意不设闸门。

不存在"因为 Agent 有本机 Shell 就绕过补丁执行器/晋升门"的路径：
本模块没有直接写文件或执行 shell 的代码，写入只经 `PatchExecutor`，晋升只经 `ReleaseManager`。

## 8. 状态与可追溯性

一次运行返回结构化记录，并落库到 `SelfDevRequest.plan["repair"]`：

```json
{
  "goal": "...",
  "trace_id": "...",
  "status": "tests_passed",
  "ok": true,
  "started_at": "...",
  "finished_at": "...",
  "max_rounds": 3,
  "context": {"files": ["backend/aiive/foo.py"], "keywords": ["foo"], "matches": 3, "truncated": false},
  "analysis": {"goal_summary": "fix foo", "test_plan": "pytest ...", "requires_schema_change": false},
  "final_patch_fingerprint": "…",
  "rounds": [
    {
      "round": 0,
      "patch_fingerprint": "…",
      "changed_files": ["backend/aiive/foo.py"],
      "applied": 1,
      "failed": 0,
      "test_ok": false,
      "test_summary": {"ok": false, "failed": 1, "results": [{"file": "...", "result": "failed", "output_tail": "..."}]},
      "failure_reason": "tests_failed"
    }
  ],
  "gate": {"ok": true, "..." : "..."},
  "promotion": null,
  "failure_reason": null,
  "final_operations": [{"operation": "modify_file", "target_file": "...", "content_sha256": "..."}]
}
```

`PatchOperation` 只记录最终补丁的操作摘要（不含文件内容），避免数据库膨胀。
复用 `Trace`（trace_id）、`SelfDevRequest`（状态）、`PatchOperation`（操作），
不新增任务框架或事件表。

## 9. API 与工具

| 入口 | 说明 |
|---|---|
| `POST /api/selfdev/repair` | `{goal, max_rounds?, promote?, run_gate?, files?}`；需要 `X-AIive-Trusted-Core-Token` |
| `GET /api/selfdev/requests/{id}` | 读取落库记录（`plan.repair`） |
| 工具 `run_selfdev_repair` | Agent 单入口；`promote` 默认 false，`high` 风险 + 需确认 |

`/repair` 与原有 `/plan`、`/{id}/apply-inactive`、`/{id}/promote` 共用同一套
Trusted Core 管理边界，不新增权限模型。

## 10. 测试

`tests/unit/backend/test_selfdev_repair.py`（28 例，全离线）：

- 补丁指纹：稳定、内容敏感、路径敏感；
- 修复循环：一轮修复成功、轮次耗尽失败、相同补丁提前停止、阻塞计划不应用、
  应用失败、规划器异常、安全门失败/异常阻断、晋升请求/失败/禁用；
- 真实 `PatchExecutor`：只改非活跃槽位，活跃槽位保持原样；
- ProjectReader：检索、相关测试、拒绝密钥/运行数据/路径穿越；
- Planner：上下文与真实错误注入提示词、`$5` 等字面量不被模板二次替换；
- 持久化：`SelfDevRequest` + `PatchOperation`；路由与工具已注册。

`tests/unit/backend/test_selfdev_repair_guard.py`（25 例，全离线）：

- 并发锁：锁被占用时第二次修复被拒（`repair_busy`）、上一次结束后重复请求可执行、
  死进程的陈旧锁可接管、越权释放不动别人的锁、运行结束必释放；
- 候选隔离：应用失败/测试失败/安全门失败后候选整树回滚到基线并清标记、
  回滚失败则隔离为不可晋升、安全门后候选被改动则阻断晋升、成功后基线+补丁+候选三方指纹绑定；
- 中断恢复：journal 记录阶段与终态、中断运行在下一次开始前回滚、无基线中断运行被隔离、
  持有者仍存活的运行被跳过、重复恢复幂等、中断中的晋升交回 Supervisor、
  已被晋升取代的运行跳过恢复；
- 晋升前核对：`ReleaseManager` 遇不可晋升标记/指纹不符即阻断，指纹一致才放行，
  无候选状态文件时保持原有行为；
- 手工 apply：锁被占用时拒绝、测试通过标记可晋升、测试失败阻断晋升。

LLM 使用脚本替身，测试运行器使用假报告，不访问网络、不需要 API Key。

## 11. 已知边界

- **不做语义级代码理解**：ProjectReader 是子串检索 + 文件读取，不构建调用图/AST；
  目标描述里的符号名越具体，检索命中越准。
- **LLM 生成的是完整文件内容，不是 diff**：大文件会显著增加 token 成本；
  `max_file_bytes` 与上下文总量上限用于兜底。
- **测试选择依赖 `TEST_MAPPING`**：新增目录若未登记映射，定向测试会 fail-closed
  （`No validation mapped`），需要人工补充映射。
- **晋升仍需显式请求**：默认 `promote=False`，闭环止于"候选通过测试与安全门"；
  自动晋升需要调用方显式开启，且仍受 ReleaseManager 闸门约束。
- **并发锁只在单机单 runtime 目录内有效**：`RepairLock` 是本地文件锁，多实例/跨机器
  并发不覆盖；跨进程边界仍由 SlotManager 的指针切换语义兜底，不做分布式锁。
- **中断恢复不重放规划**：只按 journal 回滚候选槽位并把运行标为 `interrupted*`，
  下次从零重新规划，不续跑中断时的计划。
- **隔离依赖 runtime 目录**：`candidate_state.json` 与 journal 都在 runtime 下，
  目录被整体清空则失去"不可晋升"保护。
