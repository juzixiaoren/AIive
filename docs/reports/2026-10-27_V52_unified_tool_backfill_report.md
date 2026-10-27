# V52 交付报告：后台工具结果统一回填 + Bash 日志完整性

日期：2026-10-27　范围：`backend/aiive/runtime`、`backend/aiive/tools`、`backend/aiive/task_runtime`

## 1. 目标与结论

V51 只把聊天链路（`agent_graph._tools_node`）接进了统一回填，遗留两个问题：

1. 持久任务 Worker 的工具结果走的是 `EvidenceStore._summary()` 的 1200 字符**裸切**：
   大结果被从 JSON 中间截断，模型既看不到结构化字段，也没有任何续读入口；
2. `local_shell.run_bash()` 先用 `TemporaryFile` 落盘、再用 `read(limit+1)` 只读回 64 KiB 内存，
   截断后才交给守卫——守卫拿到的"完整内容"其实已经是截断结果，落盘日志因此不完整。

本轮把三个会再次进入 LLM 上下文的入口统一到 `tool_output_guard`，并把 Bash 改成
**流式落盘 + 有界预览**。结论：

- 统一入口 3 处：聊天 `_tools_node`、后台 `EvidenceStore._summary`、聊天历史回放 `_messages_from_events`；
- Bash 日志完整性由 `StreamingLog` 保证：边产生边写盘、容量上限、原子收尾、明确 `capture_truncated`；
- 未新增第二套日志存储或分页机制，续读仍走 `read_text_file`（目录隔离 / 行内续读 / 过期清理不变）。

## 2. 真实调用链核实

| 入口 | 位置 | 结果去向 | 处理 |
| --- | --- | --- | --- |
| 聊天工具调用 | `runtime/agent_graph.py::_tools_node` | `ToolMessage` → 下一轮 LLM | V51 已接守卫，本轮增加"已守卫"幂等与流式日志复用 |
| 聊天历史回放 | `runtime/context_assembler.py::_messages_from_events`（`tool_result` 分支） | 每轮重建的 history messages | **本轮新增**守卫调用 |
| 后台任务 | `task_runtime/evidence.py::capture_result` → `_summary` | `TaskEvidence.summary` / `action.result_summary` → `TaskContextAssembler.snapshot()` | **本轮改为**守卫产物（`max_chars=1200`） |

其余入口核实为**纯展示**，不进入 LLM 上下文，按需求不改造：`api/routes_*` 的任务详情、
`runtime/action_cards.py` 的卡片渲染、`docs` 与报告。

`_bounded_value()`（`task_runtime/context_assembler.py:207`）保留：它约束的是 task_state / checkpoint /
tools 视图这类结构化上下文，不是工具输出；本轮只保证 Evidence 摘要长度 ≤1200，使其 1600 字符切片成为空操作，
不再产生"守卫产物被二次切断"的问题。

## 3. Bash 输出：流式日志

`tools/local_shell.py`：

- `Popen(stdout=PIPE, stderr=PIPE)` + 两个 `_pump` 守护线程，64 KiB 分片读取；
- 每片立刻写 `StreamingLog`（`runtime/tool_output_store.py`），内存不保留完整副本；
- `StreamingLog` 先写 `.tmp-*`，`close()` 时 flush + fsync + `os.replace` 原子改名，读者看不到半截文件；
- 容量 `SHELL_MAX_LOG_BYTES`（默认 8 MiB，单路）：超过后停止写盘但继续统计 `total_bytes`，置 `capture_truncated`；
- 日志不可用（磁盘/权限）时退化为有界内存（`SHELL_MAX_OUTPUT_BYTES`），并标 `log_error=log_write_failed`；
- 输出不超过预览长度且未触达容量上限时**不保留日志文件**（`stdout_log` 为空、`truncated=false`），
  避免每条短命令产生两个小文件；
- 结果字段：`stdout`/`stderr`（预览）、`stdout_bytes`/`stderr_bytes`（真实总量）、
  `stdout_log`/`stderr_log`、`stdout_next_offset`/`stdout_next_char_offset`（续读位置，与 `read_text_file` 的
  `offset`/`char_offset` 同语义）、`truncated`（预览是否被截）、`capture_truncated`（日志是否存全）。

不再存在"先在内存截断、再把截断结果当完整日志"的路径：内存里只有前缀预览，
磁盘上要么是完整内容，要么带 `capture_truncated=true` 明确声明不完整。

## 4. 统一回填：守卫增补

`runtime/tool_output_guard.py` 新增三项能力，不改变既有 26 例的行为：

1. `bash_streamed_log` 模式：`run_shell` 结果带 `stdout_log`/`stderr_log` 时，只回填预览 + `*_meta`
   （`total_bytes` / `capture_truncated` / `next_offset` / `next_char_offset` / `log_path`），
   **不再二次落盘**；没有日志字段（日志写失败）时回落到原有首尾/字符预算逻辑。
2. `max_chars` 参数：调用方预算。≤ 预算的结果原样返回；超限结果先按同一套规则生成摘要，
   再由 `_fit_budget()` 做结构保留收口（递归裁剪长文本 → 丢弃 hint/note → 只留定位与状态字段），
   绝不输出半截 JSON。`max_chars=0` 时沿用聊天默认 1000 字符。
3. `_is_guarded_payload()` 幂等：同时带 `truncated=true` 与不可信 `note` 的载荷直接原样返回，
   保证历史回放、图重试、任务重放不会二次截断或二次落盘。

## 5. 后台任务接入

`task_runtime/evidence.py`：`_summary()` 由"1200 字符裸切"改为
`guard_tool_output(action.capability_id, json, thread_id=task_id, trace_id=run_id, tool_call_id=action.id, max_chars=SUMMARY_LIMIT)`。

- 小结果（≤1200 字符）摘要与旧实现完全一致（仍是原始 JSON 文本），不产生日志文件；
- 大结果摘要变成结构化 JSON：`ok` / `exit_code` / `error_type` / `error` / `*_meta` / `log_path` / `hint` / `note`；
- 完整结果仍按原逻辑进对象存储（`task-evidence`，`content_size`/`content_hash` 不变），摘要变短不影响按范围读取；
- Worker 可用 `read_text_file(path=<log_path>, offset=<next_offset>, char_offset=<next_char_offset>)` 续读。

## 6. 安全与边界

- 目录隔离：日志只写 `settings.tool_output_dir`（默认 `.data/tool_outputs`）；`read_text_file` 的白名单本来就包含该目录，
  且 `realpath` 后做包含判断，符号链接与 `..` 逃逸仍被拒绝；
- 过期清理：仍由 `scheduler_daemon._tool_output_cleanup_job` 每 5h 清理超过 10h 的 `.log`/`.tmp-*`，只作用于该目录一层；
- 不越界删除：`cleanup_expired_tool_outputs` 未改动；`StreamingLog.abort()` 只删自己的 `.tmp-*`；
- 不可信标注：所有回填摘要都带 `note`（工具输出为不可信数据）；
- 不重复落盘：流式日志存在时守卫不再写第二份；已守卫载荷再次处理直接返回。

## 7. 修改文件

| 文件 | 改动 |
| --- | --- |
| `backend/aiive/runtime/tool_output_store.py` | 新增 `StreamingLog`（流式、容量上限、原子收尾）；`save_tool_output` 复用它；`_log_paths` 抽出命名规则 |
| `backend/aiive/runtime/tool_output_guard.py` | 新增 `bash_streamed_log` 分支、`max_chars`、`_fit_budget`、`_is_guarded_payload` |
| `backend/aiive/tools/local_shell.py` | stdout/stderr 改为 `_pump` 线程 + `StreamingLog`；新增 `_Capture`、`_next_position`；结果补字节数/日志/续读位置 |
| `backend/aiive/task_runtime/evidence.py` | `_summary` 改走 `guard_tool_output`（`max_chars=1200`） |
| `backend/aiive/runtime/context_assembler.py` | 历史回放 `tool_result` 分支接入守卫 |
| `backend/aiive/config.py` | 新增 `shell_max_log_bytes`、`shell_output_preview_chars`（>0 校验） |
| `backend/aiive/tools/builtin_tools.py` | `run_shell` 描述改为日志 + 续读语义 |
| `.env.example` / `README.md` | 部署说明：日志容量、预览长度、三个回填入口 |
| `docs/CODE_MAP.md` | 总表、模块行、§7.8 调用链（三入口 + 流式日志）、测试映射、文件数 |
| `tests/unit/backend/test_shell_output_capture.py` | 新增 9 例 |
| `tests/unit/backend/test_worker_tool_result_backfill.py` | 新增 6 例 |

## 8. 测试矩阵

`test_shell_output_capture.py`（真实 bash，离线）：

| # | 用例 | 证明点 |
| --- | --- | --- |
| 1 | 大输出完整落盘 | 日志 4000 行、`stdout_bytes` 与文件字节数一致、内存只有前缀预览 |
| 2 | 预览位置可续读 | `read_text_file(offset, char_offset)` 的尾段+本页 = 日志连续片段 |
| 3 | 日志容量上限 | 文件 ≤4096 字节、`capture_truncated=true`、`stdout_bytes` 仍为真实总量 |
| 4 | 日志不可用降级 | `log_error`、预览仍有内容、超内存上限时 `capture_truncated=true` |
| 5 | 双路并发 | stdout/stderr 各 2000 行都完整，无管道阻塞 |
| 6 | 超时 | 已产生输出不丢（内容在预览内时不留日志），`error_type=timeout` |
| 7 | 守卫复用日志 | 目录文件数不变（不二次落盘）、`*_meta` 与续读位置透传 |
| 8 | 后台预算收口 | ≤1200 字符、仍是合法 JSON、`log_path` 可用 |
| 9 | 小输出 | 不留日志文件、守卫原样回填 |

`test_worker_tool_result_backfill.py`：

| # | 用例 | 证明点 |
| --- | --- | --- |
| 1 | 大结果摘要 | 结构化摘要 ≤1200、`total_lines` 正确、日志含末行 |
| 2 | 小结果摘要 | 与旧实现逐字节一致、不产生日志 |
| 3 | 失败结果 | `error_type`/`error`/`exit_code` 保留 |
| 4 | 完整结果 | 对象存储 `content_size` 不变、可按范围读取 |
| 5 | 不二次截断 | 摘要 ≤1600 且与 `action.result_summary.summary` 相等 |
| 6 | 分页不被收口 | `read_text_file` 页内容原样、无 `inline_trimmed` |

## 9. 回归结果

- `pytest tests`：**1369 passed, 23 skipped**（PG / Embedding 相关按环境跳过）；
- `ruff check --select F,E9`（改动文件）：All checks passed；
- `basedpyright`（改动文件）：**0 errors, 0 warnings, 0 notes**；
- 既有 `test_local_shell.py`（30 例）与 `test_tool_output_guard.py`（26 例）全部通过，未修改任何既有断言。

## 10. 遗留风险

1. **Windows 句柄占用**：`StreamingLog.close()` 的 `os.replace` 在目标文件被其他进程打开时会失败，
   当前降级为 `log_error` + 内存预览；日志目录若有外部程序常驻扫描需注意。
2. **历史回放的旧数据**：V51 之前写入的超大 `tool_result` 事件每轮回放都会触发一次落盘（新数据幂等）。
   数据量随时间自然衰减，未做一次性迁移。
3. **对象存储与日志双份**：Evidence 的完整结果仍在 `task-evidence` 对象存储（持久、可审计），
   日志是短期续读面（10h）。两者生命周期不同，属有意保留。
4. **`read_text_file` 每页 O(n) 重扫**：V51 已记录，未在本轮处理。
5. **预览位置语义**：`next_offset` 是行号、`next_char_offset` 是行内字符偏移，与 `read_text_file` 一致；
   若将来新增按全局字符偏移读取的工具，需要换算。

## 11. 运维与回滚

- 新增环境变量：`SHELL_MAX_LOG_BYTES`（默认 8388608）、`SHELL_OUTPUT_PREVIEW_CHARS`（默认 2000），均为正数校验；
- 磁盘占用上界 ≈ 单次执行 2 × `SHELL_MAX_LOG_BYTES`，日志按 10h 保留期自动清理；
- 回滚：删掉 `local_shell` 的 `_Capture` 分支即可退回"临时文件 + 限量读取"，
  守卫与 Evidence 的改动是纯增量（`max_chars` 缺省等于旧行为）。