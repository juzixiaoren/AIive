# AIive project_plan_v26.md：Final Convergence：终局缺口审计、可观测闭环与下一轮自进化计划

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 前置要求：已完成 V1-V20 Fixed、V21-V25。  
> 阶段编号：V26  
> 本阶段定位：不新增大功能，专门检查 AIive 是否已经形成“懂用户、维护生活节奏、维护软件身体、给自己加能力”的闭环，并输出下一轮自进化计划。

---

## 1. 从 V25 到本阶段的过渡

V25 已补 proactive steward。到此，核心链路包括：

```text
Chat
Memory Revision
Task/Notification
Tool/Safe Delete
Knowledge/Retrieval
MCP Self-Bootstrap
A/B Self-Dev
Schema Safety
Self-Repair Lesson
Temporal KG
Proactive Steward
```

本阶段只做审计、报告、可观测验证，不做临时大改。

---

## 2. 产品意义

用户需要知道：

```text
AIive 当前真的完成了哪些终局目标？
哪些只是 partial？
哪些是风险？
下一步 AIive 应该怎样给自己升级？
```

---

## 3. 必须实现

### 3.1 gap_checker

新增：

```text
backend/aiive/eval/gap_checker.py
```

检查维度：

```text
1. Chat 主入口是否能触发核心能力。
2. Memory 是否支持 create/revise/supersede/forget/context resolve。
3. Context Snapshot 是否记录 injected/excluded evidence。
4. Tasks/notifications/steward 是否可从 Chat 使用。
5. Tool/safe_delete/approval 是否机械受控。
6. MCP discovery/install/activation 是否可解释可回滚。
7. Self-dev A/B/proposal/apply/test/promote/rollback 是否闭环。
8. Schema migration 是否有 expand-contract check。
9. KG 是否派生可重建。
10. UI/Inspector 是否能追踪关键事件。
```

### 3.2 final_gap_report.md

生成：

```text
docs/final_gap_report.md
```

结构：

```text
Implemented
Partial
Missing
Known Risks
User Observable Verification
AI Observable Verification
Suggested Self-Evolution Requests
```

### 3.3 Smoke Summary

新增：

```text
scripts/smoke/smoke_final_observability.py
```

它不跑所有功能，只抽样验证核心 trace 链路：

```text
user_message -> intent -> context -> action -> event -> response card -> inspector
```

---

## 4. 明确不得实现

```text
1. 不为了报告临时补功能。
2. 不伪造 implemented 状态。
3. 不运行全量 pytest。
4. 不重构核心架构。
5. 不把缺失项隐藏成 partial。
```

---

## 5. 后端实现细节

`gap_checker.py` 应读取：

```text
capabilities
events
memory_records
context_snapshots
retrieval_candidates
tasks
steward_suggestions
selfdev_requests
mcp_install_records
schema_version
graph_entities/relations
```

每个检查项输出：

```json
{
  "item": "chat_to_task_bridge",
  "status": "implemented | partial | missing | risk",
  "evidence": ["api exists", "smoke passed", "event type found"],
  "risk": "...",
  "suggested_next_action": "..."
}
```

---

## 6. 前端实现细节

Final Dashboard 可选。至少报告文件可读，并可通过 API 获取：

```text
GET /api/final-gap-report
```

---

## 7. 数据库 / 存储变更

无新核心表。可选：

```text
gap_check_runs
```

---

## 8. API 契约

```text
POST /api/final-gap-report/run
GET /api/final-gap-report
```

---

## 9. 可观测结果

用户可验证：

```text
docs/final_gap_report.md 生成，并明确列出 implemented/partial/missing/risk。
```

AI 可验证：

```bash
pytest tests/unit/backend/test_gap_checker.py -q
python scripts/smoke/smoke_final_observability.py
```

---

## 10. 验收标准 Done Definition

```text
1. final_gap_report.md 生成。
2. 报告不伪造实现状态。
3. 每个核心链路都有 evidence 或明确 missing。
4. 下一轮 self-evolution suggestions 可转化为 selfdev_request。
5. smoke_final_observability 输出 trace 链路。
6. 测试临时数据自动清理，报告文件保留。
```

---

## 11. 测试与清理

```text
1. gap_checker 单测使用 fake repository/state。
2. smoke 使用 test_run_id 创建最小样本并自动清理。
3. docs/final_gap_report.md 保留。
```

---

## 12. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v26/<run_id>/summary.json
tests/artifacts/v26/<run_id>/commands.txt
tests/artifacts/v26/<run_id>/cleanup_report.json
```

Next Phase：进入下一轮根据 `final_gap_report.md` 生成的自进化计划。
