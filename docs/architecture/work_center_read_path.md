# 工作中心读取链路

实施基线为 main `0161871f5a844c52fb20e233205a1db07ddacca9`。2026-11-22 核对本地 HEAD、origin/main 和远端 main 一致。

## 请求和可见性边界

Inbox、WorkView、Timeline 历史及增量、Evidence 和 Artifact 元数据各自在读取开始和结束执行 `forget_revision` 检查。每次尝试创建一个 `ReadVisibilityContext`，共享 Task、GoalPlan、子对象和来源的判定结果。版本变化时丢弃整个投影和可见性映射，过期 ORM 实例并从头读取一次。第二次发生变化时，REST 返回 HTTP 503、`work_visibility_changed_retry` 和 `Retry-After: 1`。PostgreSQL 使用 READ COMMITTED，读路径只执行 SELECT。会话间从数据库读取版本和判定依据。

可见性处理顺序如下。

1. 按对象类型和 ID 批量读取 `ContentProvenanceRef`，按 Event、TurnRecord、MemoryRecord 分组去重。Turn 的物理和逻辑 ID 一次展开子 Event。批量检查来源元数据、实体及选择器 Shield、Tombstone、附件上下文和 Memory ↔ Event 来源依赖。
2. 用批量 Thread Shield 与来源证明检查 Task 和 GoalPlan，保留旧 Task 合并状态限制和旧版 GoalPlan 的兼容条件。
3. 一次取回缺失的关联 Action，再一次取回缺失的结果 Evidence。按 Evidence、Action、Event 的顺序在内存中求值。Event 和 Action 即使有自己的来源证明，也必须通过其显式关联的检查。
4. Evidence、Artifact 和 Checkpoint 必须有有效来源证明。未知、悬空和查询失败的来源判定为不可见。无显式依赖的旧 Action、Event、Approval 等继续按原有兼容规则读取。

## 投影和同步

- InboxSummary 保留原有 Thread、bucket 和更新时间游标。Action 查询先按每个任务和状态执行原有扫描上限，再筛选影响当前活动、设备、审批或核对提醒的候选。排名第 33 的记录保留原有扫描警告。列表只组装主要提醒，所有合格提醒都计入总数。
- WorkDetail 使用同一套分类规则，批量验证详情的 Action、Approval、目标问题及其解决事件、Timeline、Evidence 和 Artifact。问题账本的存在性仍用于抑制旧步骤备注里的问答回退。设备只读取实际显示的节点字段。
- Timeline 的增量读取从原始序列取一条额外记录判断是否还有下一页。隐藏或内部事件仍推进原始游标。首屏和历史使用降序候选，响应按序列升序返回。WorkView 在读历史前先取原始高水位，客户端从该位置补拉增量并按 sequence 合并。
- Evidence 和 Artifact 每页默认 50 条，继续采用 `(created_at, id)` 稳定游标。候选查询只取响应所需的元数据和授权所需的 task_id。结果正文和对象存储信息在独立下载路径按需访问。
- Web、Desktop 和 Android 的共享控制器分别更新 Inbox 和 WorkView 的加载状态及错误。两类刷新各自合并失效通知，浏览分类或切换任务时通过 generation 丢弃旧请求。详情返回即渲染并开始补拉 Timeline。

## 性能验证

测试夹具在本机 SQLite 上创建 7 个任务和 6 种 Action 状态。主任务各有 6 条基础记录和 `n` 条扩展 Action、Timeline、Evidence 和 Artifact。来源含独立 Event、TurnRecord、MemoryRecord、多条 Provenance、Shield、Tombstone、32 个问题及 16 个解决事件。每次重新加载 ORM 实例，使用 SQLAlchemy `before_cursor_execute` 统计 SQL 动词和第一张 FROM 表，耗时为读取入口的本机单次墙钟耗时。修改前数据来自原始 HEAD，修改后数据来自同一夹具。表格中的每格为 SQL 条数 / 毫秒。

| n | Inbox 前 → 后 | WorkView 前 → 后 | Timeline 增量前 → 后 | Timeline 历史前 → 后 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 6,148 / 2,312.8 → 26 / 44.0 | 5,759 / 1,940.0 → 37 / 56.3 | 946 / 286.4 → 29 / 22.7 | 945 / 307.2 → 29 / 16.3 |
| 10 | 6,914 / 2,164.0 → 26 / 41.0 | 9,188 / 2,687.0 → 37 / 50.3 | 2,070 / 590.2 → 29 / 31.8 | 2,069 / 653.4 → 29 / 23.7 |
| 100 | 13,829 / 4,273.4 → 26 / 62.6 | 25,982 / 7,869.0 → 37 / 89.9 | 6,034 / 1,746.7 → 29 / 24.4 | 6,077 / 1,902.1 → 29 / 29.3 |
| 500 | 20,387 / 6,399.2 → 26 / 107.6 | 32,455 / 9,969.7 → 38 / 197.5 | 6,034 / 1,900.4 → 29 / 32.9 | 5,992 / 1,776.0 → 29 / 63.7 |

SQLite 观测到的语句全部为 SELECT。n=500 时，以下为按第一张 FROM 表统计的主要来源检查。跨表查询归入首表。

| FROM 首表 | Inbox 前 → 后 | WorkView 前 → 后 | Timeline 增量前 → 后 |
| --- | ---: | ---: | ---: |
| `content_provenance_refs` | 4,568 → 4 | 7,065 → 4 | 1,339 → 4 |
| `forget_shields` | 7,110 → 3 | 11,386 → 5 | 2,066 → 5 |
| `forget_tombstones` | 1,948 → 2 | 3,118 → 4 | 569 → 4 |
| `events` | 5,082 → 4 | 8,081 → 6 | 1,473 → 6 |
| `turn_records` | 1,440 → 1 | 2,252 → 1 | 436 → 1 |

n=500 的 SQLite 第二页历史为 29 条 SELECT，Evidence 为 27 条，Artifact 为 17 条。WorkView 比小夹具多 1 条 Action 查询，原因是 500 条场景的历史页引用了详情 Action 扫描窗口外的对象。缺失的 Action 由一个集合查询补齐。

本机 Docker PostgreSQL 16 在 n=500 下测得 Inbox 27 条 / 486.5 ms、WorkView 39 条 / 388.2 ms、增量 30 条 / 108.6 ms、历史 30 条 / 135.9 ms，全部为 SELECT。与 SQLite 的每条链路相差一条 `SELECT current_setting('transaction_isolation')`，用于首次确认 READ COMMITTED。两组本机耗时受数据库、连接与机器负载影响。生产原始实测单独记录为 Inbox 9,857 条 / 10.7 s、WorkView 3,286 条 / 约 7.4 s、Timeline 约 3.3 s。生产仅有修改前数据。

## 回归结果

- SQLite 后端相关单元和集成测试 426 项通过，最终工作中心专项复跑 31 项通过。覆盖四种规模、只读查询、分类与主要提醒、旧记录兼容、缺失或解析失败的来源、游标和真实并发 Forget。
- PostgreSQL Forget 与记忆审计回归 75 项通过。最终工作中心与来源闭包专项复跑 23 项通过，包含 PostgreSQL 查询预算和另一个事务提交 Forget 后的重验。
- 前端工作中心测试 23 项通过，Web 和 Android 生产构建通过。后端改动文件类型检查和 Ruff 的 F/E9 检查通过。
- PostgreSQL 可选向量归档用例在无 TEI 的批量运行中跳过。用本机缓存模型启动隔离 TEI 后，原始 HEAD 和本次改动均复现 `outbox_job_not_found`。用例构造的 ClaimedJob 缺少相应的持久化 Outbox 行，失败发生在向量刷新前。

复跑查询预算。

```powershell
.venv/Scripts/python.exe -m pytest tests/integration/backend/test_work_inbox_api.py -k provenance_query_budget -s
$env:AIIVE_PG_TESTS = "1"
.venv/Scripts/python.exe -m pytest tests/integration/pg/test_pg_memory_audit_access.py -s
```

## 剩余开销

- 查询次数在夹具内保持稳定，实际处理的数据量会随候选和来源关系增长。主要成本为 Action 窗口排序、最近目标问题及解决事件扫描、较长历史页的来源校验与 ORM 投影。历史和元数据每页最多扫描 512 条，稀疏可见结果会让客户端继续分页。
- Inbox 每批扫描最多 128 个候选任务，跨大量任务寻找稀有 bucket 时仍会逐批推进。每批共享已有的请求级来源判定。
- 遇到包含附件上下文等额外来源分支的记录会执行相应的批量检查。每次已提交 Forget 会使在途读取最多重做一次，版本查询本身需要统计 ForgetOperation。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `backend/aiive/task_runtime/read_repository.py` | 请求可见性上下文、批量证明和显式依赖校验、单对象兼容接口 |
| `backend/aiive/task_runtime/work_read_model.py` | InboxSummary 和 WorkDetail、只读版本重试、Timeline 游标和元数据投影 |
| `backend/aiive/task_runtime/goal_questions.py` | 批量选取提问与解决事件，共享读取上下文，保持状态推导 |
| `backend/aiive/forget/source_policy.py`、`durable_sources.py` | 来源类型分组、批量解析和公共证明入口 |
| `backend/aiive/forget/visibility_service.py`、`concurrency.py` | 批量 Shield、Tombstone 和来源扩展检查，纯 SELECT 版本校验 |
| `backend/aiive/api/routes_agent_tasks.py` | Work GET 入口以及 404、400 和 503 映射 |
| `shared/work/controller.ts` | 列表和详情独立加载、generation 与 Timeline 补拉 |
| `frontend/src/pages/TaskCenterPage.tsx`、`frontend/src/components/WorkDetail.tsx`、`apps/android/src/work/WorkCenter.tsx` | 各端列表和详情的加载及错误展示 |
| `tests/integration/backend/test_work_inbox_api.py`、`test_work_visibility.py`、`tests/integration/pg/test_pg_memory_audit_access.py` | SQL 预算、来源与分页契约、并发 Forget 回归 |
| `frontend/scripts/test-work-model.mjs` | 响应顺序、加载及错误隔离、任务切换和增量补拉回归 |
