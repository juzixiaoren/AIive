# 提示词

你要实现一个真正可自举的个人 Agent 内核。请不要做规则驱动玩具，不要只写 Markdown 文件，不要只做关键词分类器。第一版完成后，Agent 必须具备类似 Claude Code 的基础自我修改能力：能读取自己的项目文件、理解用户要求、制定修改计划、修改自己的代码/文档/记忆/能力注册表、运行测试、失败回滚、成功接管，并在后续交互中继续迭代自己。

本项目目标不是普通聊天机器人，不是固定功能助手，而是一个可以通过“交互即开发”持续成长的 Agent。

## 0. 绝对要求

第一版必须引入 LLM。

不要实现纯规则 intent classifier 作为主逻辑。规则只能作为 fallback 或 safety check。Agent 的核心决策必须由 LLM 完成。

第一版完成后，必须支持以下真实闭环：

```text
用户：你给自己的记忆加一个 hit 机制，读得越多说明越重要。

Agent：
1. 读取 docs、mind、memory、core 相关文件。
2. 判断这是 runtime/self-development 请求，不是普通记忆。
3. 制定修改计划。
4. 创建 candidate 工作区。
5. 修改 candidate 中的代码和文档。
6. 运行测试。
7. 如果测试通过，将 candidate promote 为 current。
8. 如果测试失败，保留旧版本，记录错误。
9. 回复用户真实结果。
```

这不是创建 issue 就结束。第一版必须能真正尝试修改自己的代码。

如果能力暂时做不到完整实现，也必须至少做到：

```text
能生成 patch
能应用 patch
能运行测试
能失败回滚
能把失败日志交给 LLM 继续修
```

## 1. 产品核心定义

本 Agent 是一个 Self-Evolving Personal Agent Kernel。

它由以下部分组成：

```text
LLM Brain：理解用户意图、规划修改、生成 patch、解释结果
Deterministic Kernel：文件操作、版本管理、测试、回滚、日志
Memory/Mind：用户偏好、人格、交互规则、自我模型
Capabilities：工具、MCP、工作流、能力注册表
Self-Development Loop：读代码、改代码、测试、回滚、迭代
```

核心原则：

```text
LLM 负责理解和生成修改。
Kernel 负责真实执行、验证和恢复。
Agent 必须能修改自己的代码，而不只是修改 md。
```

## 2. 第一版必须实现的核心能力

### 2.1 LLM Client

实现 `core/llm_client.py`。

必须支持至少一种真实 LLM API，优先支持 OpenAI-compatible API 格式，方便接 DeepSeek。

配置通过环境变量：

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

默认兼容：

```text
DeepSeek / OpenAI-compatible endpoint
```

LLM Client 至少提供：

```text
chat(messages, temperature, response_format=None)
```

必须支持 JSON 输出模式或强约束 JSON 解析。

不能把 LLM 接口写死成某个厂商不可替换的形式。

---

### 2.2 Project Reader

实现 `core/project_reader.py`。

Agent 必须能读取自己的项目文件。

能力包括：

```text
list_files(root, include_patterns, exclude_patterns)
read_file(path)
read_files(paths)
summarize_project_tree()
search_text(query, root)
```

默认排除：

```text
.git/
__pycache__/
.venv/
node_modules/
logs/large
releases/previous
releases/candidate
```

必须允许 LLM 请求读取更多文件，而不是一次性把整个项目塞进上下文。

---

### 2.3 Context Builder

实现 `core/context_builder.py`。

负责为 LLM 构建上下文。

上下文必须包含：

```text
用户当前请求
docs/constitution.md 摘要或全文
docs/self_improvement_protocol.md 摘要或全文
mind/persona.md
mind/user_model.md
mind/listen_policy.md
mind/self_model.md
capabilities/registry.md 摘要
相关代码文件
相关记忆文件
最近 changelog
```

不能把全项目无脑塞给 LLM。必须按请求动态选择相关文件。

---

### 2.4 LLM Decision Engine

实现 `core/decision_engine.py`。

它调用 LLM，输出结构化决策。

LLM 输出必须是 JSON，格式如下：

```json
{
  "request_type": "chat | memory_update | persona_update | listen_policy_update | capability_change | code_change | docs_update | self_improvement",
  "confidence": 0.0,
  "summary": "用户真正想要什么",
  "needs_code_change": true,
  "needs_file_update": true,
  "target_files": ["core/memory_router.py", "docs/memory_design.md"],
  "read_more_files": ["..."],
  "plan": [
    "步骤1",
    "步骤2"
  ],
  "operations": [
    {
      "type": "write_file | append_file | patch_file | create_file | create_issue | update_registry | run_tests",
      "path": "相对路径",
      "content": "内容或 patch",
      "reason": "为什么这么做"
    }
  ],
  "tests_to_run": ["pytest", "python main.py --health-check"],
  "user_response_draft": "给用户的回复草稿"
}
```

如果 LLM 判断需要读更多文件，应先返回 `read_more_files`，系统读取后再次调用 LLM。

必须实现多轮 planning：

```text
用户请求
↓
LLM 初判需要读哪些文件
↓
读取文件
↓
LLM 生成修改计划和 patch
↓
执行器执行
```

---

### 2.5 Patch Generator

实现 `core/patch_generator.py` 或在 decision engine 中实现。

LLM 必须能对具体文件生成修改。

支持两种方式：

第一种：生成 unified diff。

第二种：生成完整文件内容覆盖。

推荐支持 unified diff，必要时 fallback 为完整文件覆盖。

Patch 必须带 reason。

---

### 2.6 Update Executor

实现 `core/update_executor.py`。

负责真实执行 LLM 决策。

支持：

```text
append_file
write_file
create_file
patch_file
create_directory
update_capability_registry
create_self_development_issue
update_self_model
update_changelog
```

重要：LLM 只生成计划和 patch，不能只靠自然语言声称完成。Executor 必须真实修改文件。

---

### 2.7 Version Manager / Candidate Workspace

实现 `kernel/version_manager.py`。

必须支持候选版本机制。

最低要求：

```text
create_candidate()
apply_operations_to_candidate()
run_candidate_tests()
promote_candidate()
rollback_candidate()
```

推荐目录：

```text
releases/
├── current/
├── candidate/
└── previous/
```

如果项目一开始不在 releases/current 中，也可以把当前工作区作为 current，但必须实现 candidate 副本：

```text
工作区当前代码
↓
复制到 .jarvis_runtime/candidate/
↓
在 candidate 里改
↓
运行测试
↓
通过后同步回工作区
↓
失败则删除 candidate，不影响当前工作区
```

重点：第一版必须能在副本中修改，不能直接把生产代码改炸。

---

### 2.8 Health Check

实现 `kernel/health_check.py`。

至少检查：

```text
必要目录存在
必要 mind/docs/memory/capabilities/self_development 文件存在
Python 文件语法正确
核心模块可 import
LLM 配置存在或能 fallback 到 dry-run
CLI 可启动
pytest 可运行
```

提供命令：

```text
python main.py --health-check
```

---

### 2.9 Test Runner

实现 `kernel/test_runner.py`。

支持运行：

```text
pytest
python main.py --health-check
```

捕获 stdout/stderr，写入日志。

如果失败，失败日志必须可交给 LLM 继续修复。

---

### 2.10 Self Repair Loop

实现 `core/self_repair_loop.py`。

当 candidate 测试失败时，Agent 不应直接放弃，而应支持至少 1 次自动修复尝试：

```text
apply patch
run tests
if failed:
    collect error logs
    send logs + changed files to LLM
    ask LLM to repair
    apply repair patch
    run tests again
if still failed:
    rollback
    record issue
```

最大自动修复轮数可以配置，默认 1 或 2。

---

### 2.11 Agent Loop

实现 `core/agent_loop.py` 和 `main.py`。

CLI 必须支持：

```text
python main.py
python main.py --health-check
python main.py --self-status
python main.py --run-tests
```

交互流程：

```text
用户输入
↓
Context Builder 构造上下文
↓
Decision Engine 调 LLM
↓
如果要读更多文件，读取后再调 LLM
↓
如果只是聊天，直接回复
↓
如果是 mind/memory/docs/capability 更新，创建 candidate 并修改
↓
如果是 code change，创建 candidate 并修改代码
↓
运行测试
↓
成功 promote
↓
失败 repair
↓
仍失败 rollback
↓
更新 changelog/self_model/issues
↓
回复用户真实结果
```

---

## 3. 必须真实支持的验收场景

第一版完成后，必须能真实跑通以下场景。

### 场景 1：人格文件修改

用户输入：

```text
以后你讨论产品设想时不要一上来就保守化，先展开想象力，再讲落地路径。
```

期望：

```text
LLM 判断为 persona_update。
修改 mind/persona.md。
更新 mind/self_model.md。
更新 self_development/changelog.md。
测试通过。
回复用户真实完成。
```

---

### 场景 2：listen policy 修改

用户输入：

```text
以后我叫你大李的时候你才应，和别人聊天时别插嘴。
```

期望：

```text
LLM 判断为 listen_policy_update。
修改 mind/listen_policy.md。
写入“大李”作为显式呼叫名。
写入多人对话默认 skip。
更新 self_model/changelog。
```

---

### 场景 3：偏好更新

用户输入：

```text
我不爱喝瑞幸，我爱喝星巴克。
```

期望：

```text
LLM 判断为 preference_update。
修改 memory/preferences/coffee.md。
说明不删除瑞幸能力，只降低主动推荐优先级。
更新 self_model/changelog。
```

---

### 场景 4：真正修改代码：memory hit 机制

用户输入：

```text
你给自己的记忆加一个 hit 机制，读得越多说明越重要。
```

期望：

```text
LLM 判断为 code_change/runtime_change。
读取 core/memory_router.py、memory/index 相关文件、docs/memory_design.md。
生成修改计划。
创建 candidate。
修改 candidate 中的 memory_router 或新增 memory_index 模块。
实现最小 hit_count 机制：
    - 记忆读取时记录 hit_count
    - hit 信息保存到 memory/index/memory_hits.json 或类似文件
    - 提供 read_memory_with_hit 或等价接口
更新 docs/memory_design.md。
更新 self_model/changelog。
运行测试。
通过则 promote。
失败则 repair 或 rollback。
```

这条必须是真的代码修改，不允许只创建 issue。

---

### 场景 5：新增能力 skeleton

用户输入：

```text
你给自己加一个星巴克菜单查询工具，先做 skeleton，不需要真实联网。
```

期望：

```text
LLM 判断为 new_capability_request。
创建 capabilities/local_tools/starbucks_menu_query/。
生成 README 或 tool spec。
生成一个 skeleton 工具文件。
更新 capabilities/registry.md，状态 candidate 或 active_skeleton。
添加简单测试或 health check 能识别该 capability。
更新 self_model/changelog。
```

---

### 场景 6：测试失败后的修复

你需要在测试中模拟一个失败 patch 或至少实现机制，使得：

```text
candidate 测试失败
↓
系统把错误日志交给 LLM
↓
LLM 生成修复 patch
↓
重新测试
↓
仍失败则 rollback
```

可以不保证所有未来错误都能修复，但机制必须存在。

---

## 4. 项目目录建议

请创建或调整为如下结构：

```text
jarvis/
├── main.py
├── README.md
├── config/
│   ├── settings.example.env
│   └── llm_config.md
├── docs/
│   ├── product_vision_and_v1_architecture.md
│   ├── constitution.md
│   ├── interaction_development_protocol.md
│   ├── memory_design.md
│   ├── capability_design.md
│   └── self_improvement_protocol.md
├── core/
│   ├── __init__.py
│   ├── agent_loop.py
│   ├── llm_client.py
│   ├── context_builder.py
│   ├── decision_engine.py
│   ├── project_reader.py
│   ├── update_executor.py
│   ├── patch_generator.py
│   ├── memory_router.py
│   ├── capability_router.py
│   ├── self_model_manager.py
│   └── self_repair_loop.py
├── kernel/
│   ├── __init__.py
│   ├── version_manager.py
│   ├── health_check.py
│   ├── test_runner.py
│   └── rollback.py
├── mind/
│   ├── system_prompt.md
│   ├── persona.md
│   ├── user_model.md
│   ├── listen_policy.md
│   ├── goals.md
│   ├── daily_context.md
│   └── self_model.md
├── memory/
│   ├── preferences/
│   ├── feedback/
│   ├── events/
│   ├── summaries/
│   ├── archive/
│   └── index/
├── capabilities/
│   ├── registry.md
│   ├── local_tools/
│   ├── workflows/
│   ├── mcp/
│   ├── ui_components/
│   ├── dormant/
│   └── deprecated/
├── self_development/
│   ├── issues.md
│   ├── ideas.md
│   ├── changelog.md
│   ├── experiments/
│   └── patches/
├── .jarvis_runtime/
│   ├── candidate/
│   ├── previous/
│   └── logs/
└── tests/
    ├── test_health_check.py
    ├── test_llm_decision_schema.py
    ├── test_memory_hit.py
    ├── test_capability_registry.py
    └── test_version_manager.py
```

---

## 5. LLM 决策 Schema

必须定义 Pydantic model 或等价 schema。

例如：

```text
AgentDecision:
    request_type
    confidence
    summary
    needs_code_change
    needs_file_update
    read_more_files
    target_files
    plan
    operations
    tests_to_run
    user_response_draft
```

Operation:

```text
Operation:
    type
    path
    content
    reason
```

Operation type 至少支持：

```text
append_file
write_file
create_file
patch_file
create_directory
create_issue
update_registry
update_self_model
update_changelog
run_tests
```

LLM 输出如果不是合法 JSON，系统应要求 LLM 重试一次。仍失败则 fallback 为创建 issue，不要假装执行。

---

## 6. Prompt 设计要求

系统 prompt 必须告诉 LLM：

```text
你不是普通聊天助手。
你是这个 Agent 的自我开发大脑。
你必须根据用户请求判断应该修改哪些文件。
你可以提出代码修改，但必须输出结构化 JSON。
你不能声称完成未执行的修改。
你必须区分偏好、人格、交互规则、能力、运行机制、普通聊天。
你必须优先遵守 docs/constitution.md。
```

Decision prompt 必须包含：

```text
当前用户请求
宪法文档
自我改进协议
self_model
相关文件内容
项目树摘要
可执行操作 schema
```

Patch prompt 必须包含：

```text
目标文件原内容
用户需求
修改计划
输出 unified diff 或完整文件
```

---

## 7. Candidate 工作区要求

不能直接让 LLM 改当前项目。

流程必须是：

```text
1. create_candidate_from_current()
2. apply operations to candidate
3. run tests in candidate
4. if pass: promote_candidate_to_current()
5. if fail: repair or rollback
```

如果因为工程复杂，第一版无法完整复制整个项目，至少要实现：

```text
修改前自动备份目标文件
失败时恢复备份
成功时保留修改
```

但推荐实现 candidate 工作区。

---

## 8. 不要再做的错误事情

不要做一个基于关键词的规则机器人。

不要让 `intent_classifier.py` 成为主脑。

不要让 Agent 只会写 `issues.md`。

不要把“自举内核”理解成“文档管理器”。

不要让用户说“加 hit 机制”时只是创建 issue。

不要在没有真实修改文件时说“已完成”。

不要把 LLM 输出的自然语言当成执行结果。

---

## 9. 最小测试要求

必须实现测试，且能通过：

```text
pytest
python main.py --health-check
```

至少包括：

```text
test_health_check.py
test_version_manager.py
test_memory_hit.py
test_capability_registry.py
test_llm_decision_schema.py
```

`test_memory_hit.py` 需要测试：

```text
读取某条 memory 后 hit_count 增加。
hit_count 被写入 memory/index/memory_hits.json。
```

如果第一版在用户请求前还没有 hit 机制，仍然需要保证 Agent 后续能通过自我修改实现它。更好的做法是：直接实现一个最小 hit 机制，作为自举内核的证明。

---

## 10. 完成后的输出要求

完成后请输出：

```text
1. 实际实现了哪些模块。
2. 如何配置 LLM API。
3. 如何运行 CLI。
4. 如何运行 health check。
5. 如何运行测试。
6. 如何验证 Agent 能真实修改自己的文件。
7. 如何验证 memory hit 机制。
8. 如何验证新增 capability skeleton。
9. 当前哪些部分仍是 skeleton。
10. 下一步建议让 Agent 自己迭代的功能。
```

---

## 11. 最终验收标准

最终我会用以下方式验收：

### 验收 A：不是规则驱动

我会检查代码中是否存在 LLM decision engine。

如果只是关键词分类器，任务失败。

### 验收 B：能真实改文件

我会输入：

```text
以后你讨论产品设想时不要一上来就保守化。
```

必须真实修改 `mind/persona.md`。

### 验收 C：能真实改代码

我会输入：

```text
你给自己的记忆加一个 hit 机制。
```

必须真实修改或新增代码，而不是只写 issue。

### 验收 D：能测试和回滚

修改代码后必须运行测试。

失败时必须能回滚或恢复备份。

### 验收 E：能新增 capability skeleton

我会输入：

```text
你给自己加一个星巴克菜单查询工具，先做 skeleton。
```

必须真实创建 capability 目录和 registry 条目。

### 验收 F：回复诚实

如果没有完成，必须说没完成。

如果只是 skeleton，必须说是 skeleton。

如果只是 candidate，必须说 candidate。

不要骗我。

---

## 12. 一句话目标

请实现的不是“会写 md 的规则机器人”，而是：

> 一个带 LLM 大脑、能读写自己项目、能生成并应用 patch、能测试回滚、能通过交互继续开发自己的最小自举 Agent。
