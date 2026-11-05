# Forget 内容承载矩阵（V62.2）

本文件是 Forget 宽范围 Scrub / Purge / Verify 的审计基线。所有清理与验证实现必须
与本矩阵一致；新增内容字段时同步更新本文件与 `forget/content_scope.py`。

## 0. 边界与命名

- **tasks** 是提醒任务（`tasks` 表）；**agent_tasks** 是 Task Runtime 持久化任务，
  二者是不同模型，不能互相当作对方清理。
- **artifacts** 是普通工具产物（`artifacts.content` 文本）；**task_artifacts** 是
  Task Runtime 产物（对象存储 bucket/key + summary），二者独立。
- 单用户本地优先：`all_user_data` 覆盖用户导入的 Knowledge 与对象存储原文。
- 外部文件系统中 `documents.source_path` 指向的用户原始文件**永不删除**。
- 对象删除在数据库事务提交之后执行，并记录 `forget_actions.action_type='delete_object'`
  以便重试；数据库引用清空不等于对象已删除，Verify 必须检查对象存储实际状态。

## 1. 直接内容承载表

| 表 | 内容字段 | 关联 / 命中方式 | Selector | Mode | Scrub | Verify |
|---|---|---|---|---|---|---|
| `events` | `payload` | `thread_id` / `turn_id` / `id` / `created_at` | thread/time/all/event/turn/memory 来源 | memory_only 保留；其余清空 | `payload='{}'` | 范围内非空 payload 数=0 |
| `turn_records` | `response_payload` | `id` / `turn_id` / `thread_id` / `created_at` | 同上 | memory_only 保留；其余置 NULL | `response_payload=NULL` | 范围内非空=0 |
| `turn_event_inbox` | `payload` | `thread_id` / `created_at` | thread/time/all | memory_only 保留；其余清空 | `payload='{}'` | 非空=0 |
| `llm_calls` | `input_preview`, `output_preview` | `thread_id` / `created_at` | thread/time/all | 全部清空 | `=''` | 非空=0 |
| `context_snapshots` | `context_items`, `meta` | `thread_id` / `created_at` / `segment_id` | thread/time/all/segment | 全部清空 | `'[]'` / `'{}'` | 非空=0 |
| `working_states` | `current_objective` + 8 个语义 JSON | `thread_id` / `updated_at` | thread/time/all | 全部清空 | NULL / `'[]'` | 非空=0 |
| `tasks`（提醒） | `title`, `description`, `condition` | `thread_id` / `created_at` | thread/time/all | 全部脱敏 | `'[redacted]'` / `''` | 非空正文=0 |
| `artifacts`（普通） | `content` | `thread_id` / `created_at` / `turn_record_id` | thread/time/all/turn | 全部脱敏 | `'[redacted]'` | 非空正文=0 |
| `attention_states` | `focus_topic`, `recent_topics` | `thread_id` / `created_at` | thread/time/all | 全部清空 | NULL / `'[]'` | 非空=0 |
| `tool_operations` | `params`, `result_payload`, `effect_receipt`, `error_message` | `thread_id` / `turn_record_id` / `created_at` | thread/time/all/turn | `'{}'` / NULL | 非空=0 |
| `approval_requests` | `tool_args`, `preconditions`, `effects`, `execution_result`, `risk_snapshot`, `error_message` | `thread_id` / `turn_record_id` / `created_at` | thread/time/all/turn | `'{}'` / `'[]'` / NULL | 非空=0 |

## 2. Memory / 维护 / 检索

| 表 | 内容字段 | 关联 | Scrub | Verify |
|---|---|---|---|---|
| `memory_records` | `content`, `structured_value`, `graph_facts` | Target memory / 来源 Event-Turn 派生 | content='[forgotten]', 其余 NULL | 目标记录正文已脱敏 |
| `memory_proposals` | `content`, `structured_value`, `evidence`, `raw_payload`, `normalized_payload` | final_memory_id；`source_event_ids` JSON 成员；`source_turn_id`/`source_turn_record_id` | content=NULL, structured='[]'/'{}'、evidence='[]'、raw/normalized=NULL | 五类字段均无正文 |
| `memory_evidence` | `content_span` | memory_id；来源 event 成员 | 目标 memory 行删除；其余命中来源的行 content_span='' | 目标 memory 无行；命中来源的 span 为空 |
| `memory_maintenance_inputs` | `snapshot_json` | `memory_record_id` | NULL | 非空=0 |
| `memory_maintenance_actions` | `details`, `preconditions` | `subject_memory_record_id`；`related_record_ids` JSON 成员 | NULL | 非空=0 |
| `core_memory_blocks` | `content`, `source_memory_ids` | `source_memory_ids` JSON 成员 | 命中即删除整块（派生缓存可重建） | 无块引用目标 memory |
| `retrieval_index_entries` | `title`, `search_text`, `snippet`, `metadata` | memory_id；`thread_id`；`created_source_at` | 清正文/元数据 + `is_searchable=false` | 非空=0 且不可搜索 |
| `graph_relations` / `graph_entities` | `source_span` / 实体名 | `source_memory_id` | 关系删除；无引用实体删除 | 无目标关系/孤儿实体 |

## 3. 摘要 / 检查点 / 压缩

| 表 | 内容字段 | 命中 | Scrub（保留 ID/版本/哈希） |
|---|---|---|---|
| `segment_summaries` | goal, outcome, decisions, open_loops, entities, artifacts, important_tool_results, active_constraints, unresolved_failures, omitted_artifact_refs | `segment_id`；`source_event_ids` JSON 成员；`source_turn_ids` JSON 成员 | 文本='[redacted]'，数组='[]'；保留 source_* / source_hash / summary_version / model_id |
| `epoch_checkpoints` | current_goal, completed_milestones, open_loops, active_constraints, current_decisions, referenced_artifacts, relevant_entities, latest_verified_tool_states | `epoch_id`；`source_segment_ids` JSON 成员 | 文本='[redacted]'，数组='[]'；保留 source_segment_ids / source_hashes / version |
| `compaction_inputs` | `working_state_snapshot`（另清 `turn_manifest`/`event_manifest`） | `segment_id` | '{}' / '[]' |
| `epoch_compaction_inputs` | current_objective, open_loops, active_constraints, artifact_refs, verified_tool_states | `epoch_id` | NULL / '[]'；保留 source_segment_ids / source_hashes / snapshot_hash |

## 4. Task Runtime（`agent_*`）

| 表 | 内容字段 | 清理 |
|---|---|---|
| `agent_tasks` | title, goal, task_brief, task_state, report, last_error | 脱敏/清空；运行态任务转合法终态 `cancelled`，避免恢复逻辑继续执行；版本/预算保留 |
| `agent_runs` | input_snapshot, output_summary, error_message | 清空 |
| `agent_actions` | arguments, preconditions, effects, result_summary, evidence_refs, error_message | 清空 |
| `agent_task_events` | payload | '{}' |
| `agent_task_checkpoints` | state, plan, pending_action_refs, evidence_refs | '{}' / '[]' |
| `task_evidence` | summary, inline_payload, object_bucket/object_key | 脱敏；对象引用置空并登记删除 |
| `task_artifacts` | name, summary, object_bucket/object_key | 脱敏；对象引用置空字符串并登记删除 |

命中：`agent_tasks.thread_id`（thread）、`created_at`（time_range）、全部（all_user_data）；
turn/event 选择器经 `source_turn_record_id` 关联。

## 5. Knowledge / Object Store

| 对象 | 内容 | 命中 | 清理 |
|---|---|---|---|
| `documents` | title, object_bucket/object_key（原文对象） | all_user_data；everywhere+time_range | status='deleted', title 脱敏, 对象引用 NULL；**不删 source_path 文件** |
| `chunks` | content | 目标 document_id | 删除 |
| `knowledge_chunk_vectors` | embedding | 目标 chunk_id | 删除 |
| 对象存储 | bucket/key 内容 | 上述对象引用 | 提交后删除；仅当无任何其他行引用 |

对象删除流程：DB 事务内解除引用并插入 pending `forget_actions` → COMMIT → 逐条删除对象
→ 成功标记 done；失败标记 failed 并返回可重试。Verify 重新读取对象存储，pending/failed
或对象仍存在即失败，Shield 保持 active。

## 6. Selector × Mode 矩阵

| Selector | memory_only | history_only | everywhere |
|---|---|---|---|
| memory_ids | 清目标 memory/派生；保留 Event/Turn 正文 | 清目标 memory/派生 + 来源历史 | 清目标 memory/派生 + 来源历史 |
| turn_ids / event_ids | 清派生 memory/摘要；保留 Event/Turn | 清历史与派生 | 清历史与派生 |
| thread | 保留 Event/Turn；清派生运行态/摘要/Task Runtime | 清 Thread 全部内容（含 Task Runtime） | 同 history_only + Knowledge 不命中（无来源关系） |
| time_range | 保留 Event/Turn；清范围内派生 | 清范围内 Event/Turn/派生 | 同 history_only + 范围内 Document |
| all_user_data | 保留 Event/Turn；清全部派生 + Task Runtime | 清全部内容 | 清全部内容 + Knowledge/对象 |
| canonical_key / scope | 仅目标 memory 及可证明派生 | 同左 + 独占来源历史 | 同左 |

## 7. Verify 判定

- 逐表重新查询残留（不信 rowcount）。
- JSON 数组用成员查询（PG `jsonb_array_elements_text`；SQLite `json_each`），禁止 LIKE。
- 对象：pending/failed 动作必须为 0，且对象不存在。
- 任一残留 → `failed_retryable`，Shield 保持 active。
- 全部通过 → 宽选择器仍以 `verified_with_coarse_purge` 结束，但该状态现在有逐表证明，
  不再是“粗粒度跳过”。
