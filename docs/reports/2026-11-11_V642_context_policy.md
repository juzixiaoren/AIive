# V64.2 上下文价值策略验收（2026-11-11）

基线 `main c30a23e`，无数据库字段/表/迁移变化，无新增 LLM 调用。实现与策略说明见 `docs/architecture/Phase_1.md` 的 V64.2 章节。

## 调用链

`TurnExecutionService._load_context → ContextAssembler.assemble → 原有 Forget/Scope 过滤 → ContextCandidates.prepare → rank_source → ContextOverflowPacker.pack → render → TokenCounter.count_messages → 超限 remove_next/eviction_key → render/recount → Hard Gate → 最终 recall_pack → MemoryAccessTracker.touch → ContextSnapshotData → AgentGraph._assemble_agent_result → TurnExecutionService._rotate_snapshot → ContextSnapshot.meta.selection_trace`。

预算充足时没有删除；T3 耗尽预算仍超限时原异常保持。Attention、Questions、EpochCheckpoint、SealingBridge、Tool Definitions 固定装配，不加入可淘汰来源。Token 窗口、安全余量、检索召回、Graph 查询策略、MemoryWrite/Forget、副作用、密封与 Compaction 门禁不改。

## 淘汰示例

T0 Artifact/P3/低优先级 Core 优先于普通 T1。若 History old/new 排名百分位 0/1，Retrieval low/high 的 final_score 为 10/100、百分位 0/1，而 Graph 单条 score 为 0.01、百分位 1，则先在 old/low 中按单项 token 估计和稳定键决胜，不因为 Graph 的 0.01 小而先删 Graph。重要 Summary 和 human_identity 为 T2；T3 不参加删除。single/conflicted Graph 组继承组内最强成员的等级/百分位，整组保留或删除；不同 Scope 分组隔离。

## Snapshot 示例

以下为格式示例，estimated_tokens 展示值不是生产测量值。full_contents 仍只含最终分区内容及后续输出；被淘汰项只有元数据。

```json
{
  "selection_trace": {
    "policy_version": "v64.2.lexicographic.1",
    "candidates": [
      {
        "candidate_id": "retrieval:memory_record:low",
        "source_type": "retrieval",
        "source_id": "low",
        "source_version": "1",
        "retention_tier": 1,
        "native_score": 0.1,
        "source_rank": 2,
        "rank_percentile": 0.0,
        "estimated_tokens": 350,
        "selected": false,
        "exclusion_reason": "context_budget_exceeded"
      }
    ],
    "eviction_order": ["retrieval:memory_record:low"]
  }
}
```

source_type 是候选来源族，Retrieval 的原始 source_type 另嵌入 candidate_id 防碰撞。source_id 仍为原始源 ID。WorkingState version 只作为 source_version，epoch_id/version 不伪装成注入内容。RetrievalCandidate.selected 保持检索阶段含义。保护等级、原始得分、固定来源内排名、估计大小、删除原因及顺序可审计；不保存被删除文本。

## 验收覆盖

| 要求 | 证据 |
|---|---|
| 确定性；来源内排序 | test_context_policy 的 repeatable_order、final_score_and_graph_native_order、rank_percentiles |
| History 新 Turn / 工具配对 | test_context_overflow 的 packer_recounts_complete_turns_and_preserves_tool_pairs |
| 重要摘要；Core 角色优先级 | summary_importance_and_created_at、core_role_priority_preserves_identity_last |
| P0/P1、审批、目标、约束、已确认工具状态、副作用、运行工具永不删 | protected_state_and_optional_loop_priority、overflow_order_and_protected_state |
| 冲突原子组与 Scope | conflict_group_uses_strongest_member_and_scope、conflict_scopes_remain_separate |
| 相同价值大候选先删；未知大小 | equal_value_larger_candidate_first |
| 足够预算全部保留；必要内容超限 | budget_sufficient_leaves_everything_selected、required_content_overflow_is_unsafe |
| 真实 TokenCounter Hard Gate | production_counter_hard_gate_and_trace_matches_prompt（LiteLLMTokenCounter） |
| Prompt/trace/full_contents/touch 一致 | test_context_v641 的 only_final_injected_memories_are_touched |
| Forget 隐藏项无 trace | auto_uses_global_candidate_budget_and_complete_visible_sources（真实 Tombstone 后经过原 UnifiedRetriever） |
| AgentGraph 合并、数据库 meta 持久化 | agent_graph_merge_preserves_final_selection_trace；rotate_snapshot_persists_context_items |
| PostgreSQL JSON 契约入口 | test_pg_context_v642 两项，复用真实触达与快照落库断言 |

## 真实执行结果

- 定向：`pytest tests/unit/backend/test_context_policy.py tests/unit/backend/test_context_overflow.py tests/integration/backend/test_context_assembler_input_snapshot.py tests/integration/backend/test_context_v641.py -q` → **35 passed，11.07s**（`build/v642-final-focused.log`）。
- Ruff：全 backend `--select F,E9` 通过（`build/v642-ruff-final.log`）；新策略/Overflow/相关单测与 PG 文件默认规则通过。
- basedpyright：`basedpyright --pythonpath .venv/Scripts/python.exe` → **0 errors / 0 warnings / 0 notes**（`build/v642-types-final.log`）。
- PostgreSQL：设置 `AIIVE_PG_TESTS=1` 执行整个 `tests/integration/pg`，**126 skipped，45.69s**（`build/v642-pg-final.log`）。Docker Desktop 启动失败：Inference manager 无法访问本地 `AppData/Local/Docker/run/dockerInference` socket；无 AIIVE_PG_TEST_URL，不能启动隔离 pgvector/ParadeDB 容器。Embedding 用例另依赖不可用的本地服务。此项未取得真实 PG 通过证据，不能计为通过。未更改 Docker 配置或数据库。
- 初轮全量：未显式指定 Git Bash，误选 Windows system32/bash，**1816 passed / 17 failed / 129 skipped**，369.65s（`build/v642-full.log`）；16 项 Bash 执行失败及 1 项 supervisor candidate_start_failed。已按 V64.1 环境证据使用 `SHELL_BASH_PATH=E:/git/Git/bin/bash.exe`、`PYTHONUTF8=1` 完整复跑。

- 最终全量：同一源码下设置上述 Git Bash / UTF-8 环境，`pytest -q` → **1835 passed / 131 skipped / 0 failed / 5280 warnings，307.02s**（`build/v642-full-final.log`）。131 skip 为默认不启用的 126 项 PG 与 5 项 Windows 不支持的 POSIX 部署脚本。全量启动后新增的 AgentGraph 合并测试另在最终 35 项定向中通过；之后未改动生产代码。首轮 17 失败在此环境完整复跑中消失。
- 清理：进程 42540（最终全量）、39544（最终定向）、43560（PG 专项）对应 `tests/artifacts/validation/cleanup/<pid>/cleanup_report.json` 均为 test_exitstatus=0、status=cleaned、exists_after_cleanup=false。PG 未成功启动容器，无测试容器待清理。
- `git diff --check` 通过。实现和离线回归完成；真实 PostgreSQL 验收因上述宿主环境故障未完成。
