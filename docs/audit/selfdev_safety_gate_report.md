# 自进化安全门（Replay Eval + Kernel Contract Checks）实现报告

- 版本：V34（project_plan_v34）
- 日期：2026-10-22
- 范围：为 A/B 槽位自修改补齐“先刹车”的离线晋升安全门，满足 final_project_plan_v7 对 Eval Harness + Kernel Contract Checks 的要求。

## 1. 设计决策
- **全离线**：不调用真实 LLM / 网络 / API key。重放直接驱动**真实执行链**（ToolRegistry.execute、MemoryStore.create_record、真实 ORM 模型），仅绕过 LangGraph 的 ChatOpenAI 调用，从而保持确定性、可重复、可离线。
- **隔离会话**：`isolated_eval_session()` 建一次性 SQLite 库，并把 `aiive.tools.builtin_tools.SessionLocal` 重定向到该库，使 `@db_handler` 装饰的工具在隔离库上运行，绝不触碰现场数据。
- **fail-closed**：任一契约失败、重放崩溃/失败、候选子进程非零/不可解析 → 阻断晋升，绝不静默放行。
- **复用而非新建**：落盘复用项目既有文件态约定（`runtime/eval_runs/*.json`），无新 DB 表、无 UI 依赖；候选评测以子进程方式让候选 backend 通过 PYTHONPATH 阴影当前 `aiive`，检验“候选能跑通自身评测”。

## 2. 交付物
| 模块 | 内容 |
|---|---|
| `backend/aiive/eval/models.py` | 数据契约（ContractResult / StepResult / EvalCaseResult / EvalRunResult / GateResult） |
| `backend/aiive/eval/contracts.py` | 5 项内核契约检查 + `run_contract_checks` |
| `backend/aiive/eval/scenarios.py` | 24 个离线确定性重放场景 |
| `backend/aiive/eval/replay.py` | 隔离会话 + ReplayRunner + `run_replay_isolated` |
| `backend/aiive/eval/gate.py` | `SafetyGate.evaluate`（契约 + 重放 + 候选） |
| `backend/aiive/eval/persistence.py` | 结果落盘/读取 |
| `backend/aiive/eval/cli.py` | `python -m aiive.eval run` 入口 |
| `backend/aiive/selfdev/promote_rollback.py` | `promote()` 接入安全门（fail-closed） |
| `backend/aiive/api/routes_eval.py` | 只读查询 `/api/eval/runs`、`/runs/latest` |
| `backend/aiive/main.py` | 挂载 eval 路由 |
| `tests/unit/backend/test_eval_*.py` | 19 个用例（契约正负例 / 重放 / 闸门 / 持久化） |

### 5 项内核契约
1. `memory_single_source_of_truth`：活跃且有效的 `canonical_key` 唯一。
2. `key_events_persisted`：每个 completed TurnRecord 都有 `chat_ended` Event。
3. `outbox_idempotent`：活跃 `operation_id` 唯一；重复入队被唯一约束拒绝。
4. `safe_delete_guard`：危险路径（如 `/etc/passwd`）与越界路径拒绝，域内路径允许。
5. `context_snapshot_persisted`：每个 turn 有关联 ContextSnapshot（warning 级）。

## 3. 测试结果（真实运行）
- 全量离线套件：`876 passed / 0 failed / 14 warnings / 107.10s`（基线 857 + 新增 19）。
- 新增 eval 用例：19 passed。
- CLI 重放：`passed=24 failed=0`，覆盖工具调用、失败处理、记忆/Event/Outbox/Snapshot 写入断言、组合流。
- 闸门行为已证明：
  - 干净系统 → `ok=True`，promote 放行并切换活动槽位；
  - 现场库注入重复活跃 memory → `ok=False`，promote 阻断；
  - 候选子进程非零/不可解析 → `ok=False`，晋升阻断。
- 静态检查：`ruff check` 全绿；`basedpyright`（新增/改动文件）0 errors / 0 warnings，与既有 backend 基线一致。

## 4. 残留风险
- 候选评测只覆盖候选 `aiive/eval` 包重放，未做候选全量 diff 级回归（候选评测本身复用真实执行链，已足够作为“候选能跑通自身评测”的闸门）。
- 契约检查默认跑在隔离库；`promote` 默认不传 `live_session`，仅验证契约在干净库上成立。若需针对现场库做晋升前契约体检，可在调用 `evaluate` 时传入 `live_session`。
- PG 专属并发语义（advisory lock / SKIP LOCKED / FOR UPDATE）在 SQLite 评测库下未被覆盖，生产 PG 路径以既有集成测试为准。
- 评测库为 SQLite，与生产的 Postgres+pgvector 在少数 SQL 方言上有差异；评测刻意只覆盖不依赖向量检索的纯执行链路径。

## 5. 未做（遵循范围约束）
- 不做 Model Router / Reranker / 时序图等新能力；不重构执行链；不新增 UI。
