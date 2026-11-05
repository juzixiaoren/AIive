# Forget 内容承载矩阵（V62.2）

本文件是 Forget 宽范围 Scrub / Purge / Verify 的审计基线。所有清理与验证实现必须
与本矩阵一致；新增内容字段时同步更新本文件与 `forget/content_scope.py`。

## 0. 边界与命名

- **tasks** 是提醒任务（`tasks` 表）；**agent_tasks** 是 Task Runtime 持久化任务，
  二者是不同模型，不能互相当作对方清理。
- **artifacts** 是普通工具产物（`artifacts.content` 文本）；**task_artifacts** 是
  Task Runtime 产物（对象存储 bucket/key + summary），二者独立。
- 单用户本地优先：`all_user_data` 覆盖用户导入的 Knowledge 与对象存储原文。
- **范围冻结**：所有直接内容记录（Event/Turn/Memory/Task/Knowledge/子记录）以 Phase A
  写入 `ForgetSelectorManifest.cutoff_created_at` 为界，只清理 cutoff 之前已存在的数据；
  cutoff 之后独立产生的数据必须保留。派生记录（Summary/Checkpoint/Proposal/Retrieval/
  派生 Memory）不看自身创建时间，只要 Provenance 指向被遗忘来源，即使创建于 cutoff 之后
  也必须清理。
- 各表使用真实时间列：Event/Turn/Segment/Epoch/AgentTask/Task/Document 用 `created_at`；
  WorkingState 用 `updated_at`；RetrievalIndexEntry 用 `created_source_at`；Task 子记录
  用各自 `created_at`。
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
| `threads` | `title` | `id` / `created_at` | thread/time/all | 脱敏 | title='[redacted]' | 非空=0 |
| `agent_task_watches` | `config`, `cursor` | `task_id`（目标 Task） | 随 Task | '{}' | 非空=0 |
| `desktop_action_receipts` | `result_payload`, `error_message` | `action_id`（目标 Action） | 随 Task | NULL | 非空=0 |
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
| `retrieval_index_entries` | `title`, `search_text`, `snippet`, `metadata` | memory_id / segment_id / epoch_id / Summary / Checkpoint / thread / time / all | 清正文/元数据，`is_searchable=false`, `is_current=false` | 正文/meta 为空、Token=0、不可搜索/非当前 |
| `retrieval_index_tokens` | `token` posting | 受影响 entry_id | 物理删除 | Token 数=0 |
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

命中：`agent_tasks.thread_id`（thread，按父 Task `created_at <= cutoff`）、全部
（all_user_data）、turn/event 选择器经 `source_turn_record_id` 关联。time_range 额外按
`AgentRun/AgentAction/AgentTaskEvent/AgentTaskCheckpoint/TaskEvidence/TaskArtifact`
各自的 `created_at` 发现子记录并反向找父 Task；只清理命中子记录，不整体清空父 Task。
父 Task 的 `task_state/task_brief` 中对已清理 id 的引用替换为 `[forgotten]`；运行中
任务因此无法安全恢复时转合法终态 `cancelled`。

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

## 6. 辅助内容表（V62.3）

| 表 | 内容字段 | 命中 | 清理 |
|---|---|---|---|
| `memory_conflict_cases` | old_content, new_content, proposal_payload, evidence, graph_evidence, question, verdict_reason, decision_span, resolution_reason | old_memory_id / resolved_memory_id / proposal_id / question_event_id / thread / time / all | 文本脱敏、JSON 清空；pending/asking → stale |
| `memory_recall_runs` | request_query, scope_context, routes_executed | trace_id / RecallCandidate.memory_id / time / all | 查询与 scope 脱敏 |
| `retrieval_runs` | query | trace_id / time / all | query 脱敏 |
| `turn_event_inbox` | payload | thread / time / turn_record_id / turn_id / message_event_id | 清空（memory_only 保留） |
| `tasks`（提醒） | title, description, condition | thread / time / `source_event_id` | 脱敏，不取消仍有效提醒 |
| `approval_requests` | tool_args, preconditions, effects, execution_result, risk_snapshot, error_message | thread / turn / time / task_id / action_id | JSON/NULL 脱敏 |
| `llm_calls` | input_preview, output_preview | thread / time / trace_id | 清空 |
| `outbox_jobs` | payload 中的正文键, error_message | trace_id | 只替换 content/text/query/summary/prompt/result/output/input/message/body/preview/raw_text/raw_payload/arguments/params/tool_args 等正文键；保留 operation_id/status/幂等字段 |

## 7. Scrub ↔ Verify 字段对应

`forget/content_scrub.py` 的 `SCRUBBED_FIELDS` 与 `VERIFIED_FIELDS` 是一一对应的字段清单
（当前各 132 项）；集成测试断言 `SCRUBBED_FIELDS <= VERIFIED_FIELDS`。新增清理字段必须
同时加入两集合，并在 `verify_scope()` 中增加真实残留查询。测试
`test_every_scrubbed_field_residue_detected` 对每一个非物理删除字段逐项注入残留并断言
Verify 失败；物理删除行（Evidence/CoreBlock/Token/Chunk/Vector）单独注入验证。

未知引用结构（非数组、数组元素为对象/嵌套数组/数字等）只在本次影响范围内统计
（`count_unknown_references`），fail-closed；无关历史数据不会阻断 Forget。

## 8. Selector × Mode 矩阵

| Selector | memory_only | history_only | everywhere |
|---|---|---|---|
| memory_ids | 清目标 memory/派生；保留 Event/Turn 正文 | 清目标 memory/派生 + 来源历史 | 清目标 memory/派生 + 来源历史 |
| turn_ids / event_ids | 清派生 memory/摘要；保留 Event/Turn | 清历史与派生 | 清历史与派生 |
| thread | 保留 Event/Turn；清派生运行态/摘要/Task Runtime | 清 Thread 全部内容（含 Task Runtime） | 同 history_only + Knowledge 不命中（无来源关系） |
| time_range | 保留 Event/Turn；清范围内派生 | 清范围内 Event/Turn/派生 | 同 history_only + 范围内 Document |
| all_user_data | 保留 Event/Turn；清全部派生 + Task Runtime | 清全部内容 | 清全部内容 + Knowledge/对象 |
| canonical_key / scope | 仅目标 memory 及可证明派生 | 同左 + 独占来源历史 | 同左 |

**Cutoff 列（适用于所有行）**：直接记录只命中 `created_at/updated_at <= cutoff` 的行；
派生记录（Summary/Checkpoint/Proposal/Retrieval/派生 Memory）按 Provenance 命中，
不受自身创建时间限制。all_user_data 同样冻结在 Phase A cutoff。

## 8. 非内容表审计结论

以下表已审计但不属于用户正文承载位置，不纳入 Scrub（不标记为“已清理内容”）：

- `memory_recall_candidates` / `retrieval_candidates`：仅 id/分数/路由/排除原因。
- `retrieval_index_generations` / `retrieval_index_runs` / `compaction_runs` /
  `checkpoint_runs`：状态、游标、计数、错误摘要；不含用户正文。
- `auth_owner`、`desktop_nodes`、`capabilities`、`selfdev_*`、`thread_execution_lease`、
  `turn_recall_*` 等运行/配置表：无用户内容字段。
- `memory_vector_projection`：仅向量与版本；Memory 被遗忘时由 `_scratch_batch` 的
  `memory_vector_projection` 删除逻辑处理（Phase A 对显式 ID 已删除）。

## 9. Outbox Payload 契约与异步阻断（V62.4）

契约模块：`backend/aiive/worker/outbox_payload_contract.py`，Scrub / Verify / Worker 共用。

| job_type | 正文/需清理字段 | 保留的引用/状态/幂等字段 |
|---|---|---|
| `memory_extraction` | user_message, reply | source_event_ids, source_turn_record_id, source_turn_id, thread_id, assistant_event_id, message_source, extraction_mode, operation_id |
| `goal_event_dispatch` | event_payload | event_id, event_type, source, thread_id, occurred_at |
| `goal_trigger_decision` | trigger_event_payload, reason, trigger_event_type | task_id, step_id, trigger_key, trigger_event_id, fired_at, trace_id, pending_event_dedupe_key |
| `reminder_delivery` | title, content | task_id, thread_id, task_type, scheduled_at, operation_id, schema_version |
| `agent_task_run` | goal, reason, failure_reason, task_title, summary, query | task_id, trigger, trace_id, schema_version |
| `memory_conflict_question/reply/cleanup` | question, message, content, title, reason | conflict_case_id, thread_id, status |
| `tool_operation` | name, params, result, error, tool_args, arguments | tool_call_id, idempotency_key, operation_id |
| `improvement_discovery` | title, content, message, goal, content_preview | operation_id, schema_version |
| `segment_sealing / epoch_checkpoint / epoch_rollover / memory_maintenance` | summary, content, goal, reason | segment_id, epoch_id, source_id, operation_id |

- 发现：trace_id + operation_id + `payload` 文本中的目标 ID（结构化 ID 匹配，不做内容关键词匹配）。
- 清理：按 job_type 递归替换正文键为 `[redacted]` / `{}` / `[]` / NULL，写入 `forget_content_redacted=True`；保留定位与幂等字段。
- 异步阻断：`handle_memory_extraction` 在 LLM 调用前（Phase A 之后）与持久化写入前（Phase C）双重调用 `_extraction_blocked_reason()`；命中 Shield/Tombstone 或已 Scrub 载荷时收敛 IngestionRun 为 succeeded 并直接 COMPLETED，不调用模型、不写回记忆。`goal_event_dispatch` / `goal_trigger_decision` / `reminder_delivery` Handler 遇到 `forget_content_redacted` 直接完成。
- 无关 Job 不被取消，payload 保持不变。

## 10. 范围边界与发现（V62.4）

- Turn→子 Event：显式 turn_ids 与间接 Turn 均按 `Event.created_at <= cutoff`；间接 Turn 在 time_range 下还须逐个 Event 落在时间窗口。`raw_event_ids` 只含实际命中原始 Event；派生 Summary/Memory 仍按 Provenance 清理。
- Task Runtime：`ContentScope` 独立维护命中的 AgentTask 与六类子记录 ID 集合；每条子记录按自身 `created_at` + cutoff/time_range 判断。父 Task 命中只清父正文，子记录命中只清子记录；未命中父任务仅脱敏 `task_state/task_brief` 引用，引用变化时运行态任务才转 `cancelled`。
- 未知 JSON：`unknown_reference_details()` 先按本次范围确定候选（受影响集合 + 「目标 ID 在引用数组文本中出现」+ all_user_data 按 cutoff），再检查数组结构；对象/嵌套数组/数字/布尔等元素 fail-closed。无关 Thread 且不含目标 ID 的异常数组不阻断本次 Forget。

## 11. Verify 判定

- 逐表重新查询残留（不信 rowcount）。
- JSON 数组用成员查询（PG `jsonb_array_elements_text`；SQLite `json_each`），禁止 LIKE。
- 对象：pending/failed 动作必须为 0，且对象不存在。
- 任一残留 → `failed_retryable`，Shield 保持 active。
- 全部通过 → 宽选择器仍以 `verified_with_coarse_purge` 结束，但该状态现在有逐表证明，
  不再是“粗粒度跳过”。
- PostgreSQL 实测：`tests/integration/pg/test_pg_forget_scope_integrity.py` 覆盖 cutoff
  新数据保留/派生清理、Task 子记录 time_range、Conflict/Recall/Retrieval/Outbox、
  未知 JSON fail-closed、真实 UnifiedRetriever 前后召回、跨表字段残留注入；
  SQLite 另有 132 字段逐项注入。
