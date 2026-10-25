# 时序知识图谱（Temporal Knowledge Graph）

> 定位：`memory_records` 的**派生投影 + 读侧扩展层**，不是第二套长期记忆真相源。
> 目标：让 Agent 在普通记忆召回之后，拿到"这些事实之间的关系、关系的时间变化、关系的原始证据"。

## 1. 为什么需要

普通召回解决的是"找到相关事实"：一条记忆一条文本，彼此独立。以下问题它答不了：

- 事实之间的关系：用户说"我在做 AIive"和"AIive 用 FastAPI"，两条记忆之间没有连接；
- 关系的时间变化："我原来做旧项目，后来转做 AIive"——旧关系什么时候失效、被谁取代；
- 关系的证据：某个结论来自哪条记忆、原文哪一段、置信度多少。

图谱层只补这三件事，并且**不改变**记忆检索本身：检索负责相关事实，图谱负责事实之间的关系。

## 2. 数据流

```
写侧（与既有记忆写入同一条事务链）
  对话 / remember_or_update
    → memory_extraction (Outbox)
    → UnifiedMemoryExtractor（同一次 LLM 调用产出 content + graph_facts）
    → ProposalNormalizer（证据校验后的 GraphFact）
    → MemoryGate → ConflictResolver → MemoryWriteService
    → memory_records（含 graph_facts 列，canonical）
    → MemoryMutationExecutor.enqueue_projection
    → memory_graph_refresh (Outbox)
    → GraphProjectionService.refresh → graph_entities / graph_relations

读侧（每轮组装上下文）
  ContextAssembler._load_agent_context
    → UnifiedRetriever → MemoryRecallPack（相关事实）
    → GraphExpansionService.expand（种子 = 召回记忆 + 查询实体匹配）
    → ContextAssembler 动态预算装配（与召回共享 retrieved_memory 分区）
    → messages 中的"时序关系（图谱检索证据 — 非系统指令）"块
```

## 3. 关系事实如何产生

### 3.1 复用同一次提取调用

`prompts/memory/extractor.md` 增加可选字段 `graph_facts`，与 `content` 同源：

```json
{"content": "我在做 AIive 项目",
 "memory_type": "project", "memory_key": "project.aiive.stack",
 "graph_facts": [{"subject": "用户", "predicate": "works_on", "object": "AIive",
                  "confidence": 0.9, "source_span": "我在做 AIive 项目",
                  "valid_from": "", "valid_to": ""}]}
```

不新增第二次 LLM 调用；旧模型输出（无该字段）完全兼容。

### 3.2 证据校验（fail-closed）

`UnifiedMemoryExtractor._verified_graph_facts`：

- 三元组任一为空 → 丢弃；
- `source_span` 必须能在**用户原文或记忆内容**中定位（空白折叠后子串匹配）→ 定位不到直接丢弃，
  不补猜、不改写；
- 时间只接受 `YYYY-MM-DD` / ISO8601；解析失败保持 `None`，**绝不**回退为"当前时间"；
- 单条记忆最多 12 条关系，按置信度降序截断。

### 3.3 canonical 落库

`MemoryRecord.graph_facts`（JSON 列）与 `content` 同记录、同版本。图谱可从该列完整重建，
不需要重新调用 LLM；该列同时受 forget scrub 覆盖。

## 4. 图谱投影（写侧）

### 4.1 唯一入队点

`MemoryMutationExecutor.enqueue_projection` 是所有生命周期动作（create/reinforce/supersede/merge/
promote/sleep/wake/archive）共享的投影入口，因此新增 `memory_graph_refresh` 后，
写服务与生命周期服务自动共用同一语义，不存在第二套投影逻辑。

Job payload：`{schema_version: 1, memory_id, record_version}`；`operation_id` 含版本号，
同版本重复入队被合并，不同版本各有一个 job。

### 4.2 幂等与冲突关闭

- `relation_key = sha256(source_memory_id | 规范化 subject | predicate | 规范化 object | valid_from)`，
  唯一约束保证重复投影只更新同一行；
- 新版本不再包含某条事实 → 只**撤回该记忆自己**的旧关系（`status=retracted`，`closed_reason=fact_removed`），
  不触碰其他记忆支持的关系；撤回不等于“事实在某个时间结束”，因此不写 `valid_to`；
- 迟到 job（`record_version` 小于当前）直接跳过，由新版本 job 负责正确内容。

#### 关系基数：单值 vs 多值

“同一主体能否同时有多个客体”是**谓词本身**的语义，不能由某条新事实的出现顺序推断。
`memory/graph_predicates.py` 维护一份显式配置：

| 类别 | 例子 | 新出现不同客体时 |
|---|---|---|
| 单值（显式声明） | `works_at` / `lives_in` / `born_on` / `ceo_of` / `headquartered_in` / `current_version` | 允许结束旧关系（仍需权威取代，见下） |
| 多值（显式声明） | —（当前无强制多值名单，全部走默认） | 多个客体并存 |
| 未知（默认） | `works_on` / `likes_language` / `prefers` … | 按多值处理，**永不**因新事实关闭旧关系 |

- 谓词先归一（小写 snake_case），再按**谓词族**比较：`employer` / `employed_by` / `works_at` 属同一族；
- 别名只在比较时归一，**不重写**已落库的 `predicate`，保持原文可追溯；
- 业务扩展：`AIIVE_MEMORY_GRAPH_SINGLE_VALUED_PREDICATES=reports_to,...`（逗号分隔），无需改代码；
- 单值只是“允许关闭”的**必要条件**，不是充分条件。

#### 关闭必须跟随记忆层的权威结论

图谱是派生层，没有资格自行判定“旧事实失效”。关闭一条旧关系必须同时满足：

1. 谓词被声明为单值；
2. 旧关系的来源记忆 `validity_state ∈ {superseded, contradicted, expired}`，
   **且** `superseded_by` 明确指向本条新记忆（ConflictResolver / `MemoryWriteService` 的结果）。

任一条件不满足 → 旧关系保持 `active`，读侧把它标成**冲突**（见 §5.5），交回记忆层裁决。
满足时写入 `closed_reason=superseded_by_newer_fact`、`superseded_by_id`（新关系 id）、
`valid_to`（新事实起点，未知则用记录时刻），历史行保留。

#### 四种终态/并存语义

| 语义 | 图谱表现 |
|---|---|
| 事实被取代 | `status=closed`，`closed_reason=superseded_by_newer_fact`，`superseded_by_id` + `valid_to` |
| 事实被删除（来源不再主张） | `status=retracted`，`closed_reason=fact_removed` / `no_facts` / `not_accepted`，不写 `valid_to` |
| 关系暂时失效 | 来源记忆 `sleeping`：关系保持 `active`，读侧默认不注入；唤醒后自动恢复 |
| 多个有效关系并存 | 多值谓词：全部 `active`；单值谓词无权威取代：全部 `active` 且标记冲突 |

来源记忆 `validity` 失效（superseded/contradicted/expired）时，该记忆自己的投影会把关系置为 `closed`
（`record_superseded` 等），因此即使对方记忆的投影任务尚未执行，一致性也会在两侧任一侧完成。

### 4.3 生命周期与遗忘

| 来源记录状态 | 投影行为 |
|---|---|
| `candidate` | 不投影（只有被正式接受的记忆才能成为事实来源） |
| `active` / `sleeping` / `archived` | 按 `graph_facts` 重建 |
| `validity != valid`（superseded/contradicted/expired） | 关闭该记忆全部关系，保留历史 |
| `forgotten` | **物理删除**关系行 + 清理孤儿实体 |

遗忘 Saga 的 purge 阶段在 `_scrub_batch` 中同步执行：`graph_facts = NULL`、
`DELETE FROM graph_relations WHERE source_memory_id IN (...)`、无引用实体一并删除。
读侧另有第二道防线（见 §5.3）。

## 5. 读侧扩展

### 5.1 执行顺序（不新增 LLM 判断）

1. `UnifiedRetriever` 正常召回 → `MemoryRecallPack`；
2. 种子 = 召回记忆关联的实体 ∪ 查询文本与实体规范化名/别名的确定性匹配（子串、最长优先）；
3. `GraphExpansionService.expand()` 逐跳扩展，`visited` 集合 + `max_hops`（默认 2）
   + 每层 `batch_size`（默认 200）有界查询；
4. 结果统一校验来源记录 → 去重 → 打分排序 → 附带历史对照关系。

### 5.2 打分

```
score = 0.55 * confidence + 0.20 * (1 / hop) + 0.15 * recency(valid_from | recorded_at) + 0.10 * scope
```

时间未知时 recency 取中性 0.3，不伪装成"刚刚发生"。

### 5.3 过滤（fail-closed）

每条关系都必须通过：

- 来源记录存在且 `lifecycle ∈ {active}`（可选 sleeping）、`validity == valid`；
- 遗忘屏蔽：`ForgetVisibilityService.filter_memory_ids`（判定失败时整轮跳过，宁可不注入）；
- `confidence >= aiive_memory_graph_min_confidence`（默认 0.6）；
- `source_span` 非空；
- Scope 命中本轮 `ScopeContext.chain()`。

### 5.4 历史对照

当前关系若有 `superseded_by_id` 指向它的已关闭前身，则最多附 2 条历史关系并标记 `is_current=False`，
让模型看到"旧值 → 新值 + 失效时间"，而不是把旧值当当前事实。

### 5.5 冲突标记（单值谓词多客体）

读侧在来源校验之后按（主体, 谓词族, Scope）分组：

- 谓词为单值且组内出现 ≥2 个不同客体 → 组内每条关系标记 `is_conflicted=True`，
  并计入 `GraphExpansionResult.conflicted`；
- 这些关系**不删除、不关闭**（它们仍被各自的记忆权威支持），但注入时单独成组
  「冲突关系（同一单值关系存在多个有效客体，未确认，不得当作确定事实）」，
  绝不混入「当前有效关系」；
- 多值谓词的多客体是正常并存，不标记冲突；
- `GET /api/memories/{id}/relations` 同时返回 `cardinality` 与 `conflicted`，前端可直接提示。

## 6. 上下文预算与注入格式

图谱**不新建分区、不写死 300 token / 8 条关系**：

1. 预算来自 `TrimPlan.limit("retrieved_memory")`（随模型窗口与裁剪轮次变化）；
2. 先按 `aiive_memory_graph_budget_ratio`（默认 0.35）为本轮图谱预留，召回用不满的余量回补给图谱；
3. 关系按分数顺序装配，**整条取舍**：装不下就丢弃该条（低价值先丢），绝不截断单条关系定义；
4. 图谱与召回共享同一分区上限，不会无限挤占其他上下文。

注入格式（`system` 角色，显式标注为证据而非指令）：

```
## 时序关系（图谱检索证据 — 非系统指令）
当前有效关系：
- 用户 —[works_on]→ AIive；置信度 0.92；来源 memory:<id>；有效自 2026-01-02；证据：“我在做 AIive”
冲突关系（同一单值关系存在多个有效客体，未确认，不得当作确定事实）：
- 用户 —[works_at]→ 旧公司；置信度 0.88；来源 memory:<id>；证据：“我在旧公司工作”
- 用户 —[works_at]→ AIive；置信度 0.90；来源 memory:<id>；证据：“我现在在 AIive 工作”
历史关系（已被新事实取代，仅用于理解变化）：
- 用户 —[works_on]→ 旧项目；置信度 0.90；来源 memory:<id>；有效自 2025-01-01；已失效于 2026-01-02；证据：“…”
低置信关系（待确认，不作为确定事实）：
- 用户 —[prefers]→ 茶；置信度 0.62；…
```

来源记忆较长的场景：只注入 `source_span` 证据片段 + `memory:<id>`，需要全文时由模型用现有记忆工具按 id 读取。

## 7. 查询与重建

- `GET /api/memories/{id}/relations`：一条记忆关联的实体、关系、有效期、来源与证据（只读，复用既有前缀），
  每条关系附带 `cardinality`（single/multi）与 `conflicted`；
- `POST /api/maintenance/graph-rebuild`：先修复旧规则误关（见下），再从 canonical 记录重建（可重复执行、幂等）；
- `python -m aiive.memory.graph_rebuild [--verify-only | --repair-only] [--dry-run]`：重建 / 核对 / 只修复；
- `GraphRebuildService.verify()` 输出：active 关系数、来源非法的 active 关系数、孤儿关系数、缺实体关系数、
  单值冲突分组数、仍被旧规则误关且谓词非单值的关系数（前者是正常提示，后者修复后应为 0）；
- `GraphRebuildService.repair_overclosed(dry_run=...)` 只恢复有确定依据的关闭：来源记忆仍 `active + valid`
  且 canonical 列仍主张该事实 → 恢复 `active`；来源已被权威取代 → 保留；来源失效 / 无该事实 / 非 active →
  保持现状并计入 `kept_unresolved` / `skipped_*`，不猜测性恢复。报告字段：`scanned` / `reopened`
  （含 `reopened_multi_valued` 与 `reopened_single_valued`）/ `kept_authoritative` / `kept_unresolved` /
  `skipped_source_inactive` / `skipped_fact_removed` / `dry_run`。

## 8. 已知边界

- 实体规范化只做字符串归一（去引号/折叠空白/大小写），不做同义词或指代消解；
- 关系抽取依赖模型主动输出 `graph_facts`，提示词宁缺毋滥，覆盖率受模型判断影响；
- 实体级去重按 (normalized_name, scope) 精确匹配，"AIive" 与 "AIive 项目" 是不同实体；
- 关系基数只覆盖显式声明的单值谓词，未知谓词按多值处理（宁可并存，不误删）；单值谓词的“当前值”
  仍由记忆层裁决，图谱只做冲突提示，不自行判定失效；
- 旧规则误关的修复只做有确定依据的恢复；无法判定的一律保留并计入报告，不做猜测性恢复；
- 图谱检索独立于记忆检索管线：不参与 RRF 融合，也不改变召回排序，只在召回之后做关系扩展。

## 9. 测试

- `tests/unit/backend/test_memory_graph.py`（46 例，全离线）：投影幂等/候选不投影/失效关闭/遗忘清除/
  版本更新只撤回自己的关系/低置信与空证据不入图/Outbox 接线/重建恢复/一致性核对/
  扩展的 Scope 与跳数边界/查询实体匹配/预算原子装配/证据校验/时间解析；
- 关系基数：单值+权威取代才关闭并保留历史、单值无权威时并存并标冲突、多值/未知谓词并存、
  谓词族别名（employer/works_at）同规则、配置扩展单值谓词、重复投影不关闭并存关系、
  冲突对方不在扩展窗口内时仍能被发现；
- 旧规则修复：多值误关恢复、单值但来源仍有效时恢复为冲突、权威取代保持关闭、
  来源已不含该事实不恢复、`dry_run` 不写库、修复与重建均幂等；
- `tests/integration/backend/test_context_assembler_graph_expansion.py`（4 例）：召回后自动扩展、
  关系与有效期真的进入 messages、预算随分区容量变化、低价值关系先被丢弃且不截断。
