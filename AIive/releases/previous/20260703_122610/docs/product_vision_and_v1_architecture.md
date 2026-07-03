# 自进化个人 Agent：第一版产品与架构文档

## 1. 项目目标

本项目目标不是开发一个普通聊天机器人，也不是开发一个固定功能的语音助手，而是构建一个具备长期记忆、人格演化、能力沉淀、自我维护、自我扩展能力的个人管家型 Agent。

第一版不追求完整 Jarvis，不追求手机端常驻语音、不追求复杂主动提醒、不追求咖啡点单、地图导航等完整业务功能。第一版只实现一个最小自举内核，使 Agent 具备后续“通过交互自我成长”的基础能力。

第一版的核心目标是：

用户可以直接和 Agent 说：

- “以后你说话别这么像产品经理。”
- “我不爱喝瑞幸，我爱喝星巴克。”
- “以后我叫你大李的时候你才应，和别人聊天时别插嘴。”
- “你给自己的记忆加一个 hit 机制。”
- “你给自己加一个 sleep 功能，定期整理低频记忆。”
- “你帮自己加一个星巴克查询工具。”

Agent 能判断这些话分别应该沉淀到哪里：记忆、人格、交互规则、能力注册表、待开发任务、候选代码修改，或者普通聊天回复。

换句话说，第一版要实现的不是“很多能力”，而是：

> 一个能够理解自己结构、维护自己文件、把用户交互转化为长期改进的自举 Agent 框架。

## 2. 核心产品理念

### 2.1 交互即开发

传统流程是：

用户提出需求 → 程序员打开 Claude Code → 修改 Agent 项目 → 运行测试 → 部署。

本项目希望变成：

用户直接对 Agent 说出需求 → Agent 判断这是偏好、人格、交互规则、能力、工作流还是系统机制 → Agent 修改自己的文件、能力或候选代码 → 测试后生效。

第一版只需要完成这个机制的骨架，不需要实现所有高级功能。

### 2.2 Agent 拥有自己的软件身体

Agent 不只是运行在代码之上，它还应该能理解和维护自己的软件身体。

软件身体包括：

- `mind/`：人格、用户模型、交互规则、自我模型。
- `memory/`：用户偏好、事件、反馈、总结、长期记忆。
- `capabilities/`：工具、工作流、MCP、UI 组件、能力注册表。
- `self_development/`：自我改进任务、想法、补丁、实验和 changelog。
- `docs/`：Agent 的世界观、架构说明、成长规则。
- `kernel/`：最小稳定内核，用于启动、健康检查、候选版本、回滚。
- `core/`：Agent 主循环、上下文构造、工具路由、记忆路由、能力路由。

### 2.3 先会成长，再长功能

不要第一版就实现完整语音识别、手机端、点单、地图、新闻等能力。第一版要让 Agent 具备“以后自己长出这些能力”的基础。

第一版成功标准不是“功能多”，而是：

- Agent 能读取文档理解项目目标。
- Agent 能根据用户输入判断沉淀位置。
- Agent 能修改 `mind/` 和 `memory/` 文件。
- Agent 能维护 `capabilities/registry.md`。
- Agent 能记录自我开发任务。
- Agent 能创建候选版本。
- Agent 能运行自检。
- Agent 能在候选版本通过后切换，失败时回滚。
- Agent 能更新 `self_model.md` 和 `changelog.md`。

## 3. 第一版范围

### 3.1 第一版必须实现

#### 1. 文档读取能力

Agent 启动时或需要时，能够读取 `docs/` 下的说明文档，理解项目目标和当前开发规则。

需要优先读取：

- `docs/product_vision_and_v1_architecture.md`
- `docs/constitution.md`
- `docs/interaction_development_protocol.md`
- `docs/memory_design.md`
- `docs/capability_design.md`
- `docs/self_improvement_protocol.md`

#### 2. Mind 文件维护能力

Agent 能读取和修改：

- `mind/persona.md`
- `mind/user_model.md`
- `mind/listen_policy.md`
- `mind/goals.md`
- `mind/self_model.md`
- `mind/daily_context.md`

这些文件用于记录人格、用户偏好、交互风格、呼叫规则、Agent 对自己的理解。

#### 3. Memory 文件维护能力

Agent 能读写：

- `memory/preferences/`
- `memory/feedback/`
- `memory/events/`
- `memory/summaries/`
- `memory/archive/`

第一版不强制实现复杂向量检索，但需要具备基础文本读写和简单索引能力。

#### 4. 能力注册表维护能力

Agent 能维护：

- `capabilities/registry.md`

每个能力至少记录：

- 名称
- 描述
- 状态：active / dormant / deprecated / candidate
- 适用场景
- 调用方式
- 最近使用时间
- hit_count
- 用户偏好关联
- 备注

第一版可以不真正实现复杂 MCP，但必须能为未来能力注册预留结构。

#### 5. 交互分类能力

Agent 收到用户输入后，先判断输入属于哪类：

- `ordinary_chat`：普通聊天或普通问答。
- `preference_update`：用户偏好更新。
- `persona_update`：人格和表达风格调整。
- `listen_policy_update`：呼叫规则、响应规则、skip 规则。
- `memory_update`：明确要求记忆或遗忘。
- `new_capability_request`：新增工具、MCP、工作流、UI 组件。
- `runtime_change_request`：修改运行机制，例如 hit、sleep、索引、调度。
- `self_development_request`：让 Agent 规划或继续自我开发。
- `bug_report`：用户报告 Agent 行为不对。
- `documentation_update`：用户要求更新文档。

#### 6. 自我开发任务队列

Agent 能维护：

- `self_development/issues.md`
- `self_development/ideas.md`
- `self_development/changelog.md`

当用户提出暂时不能立即完成的功能时，Agent 不应假装完成，而应创建 issue，说明需求、原因、涉及模块、建议实现路径和优先级。

#### 7. Candidate 修改机制

第一版需要支持：

- 复制当前版本到 candidate。
- 在 candidate 上修改。
- 运行基础 health check。
- 通过后 promote。
- 失败后保留 current，记录错误。

第一版可以先用最简单的目录复制和命令式自检，不要求复杂 CI/CD。

#### 8. Health Check

第一版至少检查：

- 必要目录存在。
- 必要文档存在。
- `mind/` 必要文件存在。
- `memory/` 必要目录存在。
- `capabilities/registry.md` 存在。
- `self_development/` 必要文件存在。
- Agent 主程序可以启动。
- 配置文件格式无明显错误。

#### 9. Self Model 更新

Agent 在新增能力、修改人格、写入重要记忆、创建 issue、完成自我修改后，应更新 `mind/self_model.md`。

`self_model.md` 记录：

- 我当前有哪些能力。
- 我当前有哪些限制。
- 最近新增了什么。
- 最近用户对我有什么反馈。
- 哪些能力处于 active。
- 哪些能力处于 dormant。
- 我下一步计划改进什么。

### 3.2 第一版暂不实现

以下功能第一版可以只写文档和 issue，不需要实现完整功能：

- 手机端 App。
- 后台常驻语音监听。
- VAD、ASR、声纹识别、说话人分离。
- 环境语音 1 天缓存。
- 点单瑞幸、星巴克等真实 MCP。
- 地图、天气、新闻主动推送。
- 复杂长期向量记忆。
- 多 Agent 协作。
- 完全无人监督的自我重构。
- 自动修改手机端原生代码。
- 完整 Web UI。

这些应作为后续能力由 Agent 自己逐步开发。

## 4. 推荐目录结构

```text
jarvis/
├── README.md
├── docs/
│   ├── product_vision_and_v1_architecture.md
│   ├── constitution.md
│   ├── interaction_development_protocol.md
│   ├── memory_design.md
│   ├── capability_design.md
│   └── self_improvement_protocol.md
├── core/
│   ├── agent_loop.py
│   ├── context_builder.py
│   ├── intent_classifier.py
│   ├── memory_router.py
│   ├── capability_router.py
│   ├── tool_router.py
│   └── self_model_manager.py
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
├── kernel/
│   ├── supervisor.py
│   ├── version_manager.py
│   ├── health_check.py
│   └── rollback.py
├── releases/
│   ├── current/
│   ├── candidate/
│   └── previous/
├── logs/
│   ├── runtime.log
│   ├── development.log
│   └── health_check.log
└── tests/
    ├── test_health_check.py
    ├── test_intent_classifier.py
    ├── test_memory_router.py
    └── test_capability_registry.py
```

第一版可以根据实际工程简化，但不得丢失以下核心目录：

- `docs/`
- `mind/`
- `memory/`
- `capabilities/`
- `self_development/`
- `kernel/`

## 5. 交互分类与沉淀规则

Agent 收到用户输入后，应先判断它是否需要沉淀。

### 5.1 普通聊天

示例：

“你怎么看这个想法？”

“这个架构合理吗？”

处理：

直接回答，不写入长期记忆，除非用户明确要求记住或内容明显长期有用。

### 5.2 偏好更新

示例：

“我不爱喝瑞幸，我爱喝星巴克。”

“以后咖啡优先推荐星巴克。”

处理：

写入 `memory/preferences/coffee.md`。

必要时更新 `mind/user_model.md`。

不要删除已有工具，只调整偏好和主动推荐策略。

### 5.3 人格更新

示例：

“你别说得像产品经理。”

“以后跟我讨论这种概念时大胆一点，不要太保守。”

处理：

写入 `mind/persona.md` 或 `mind/user_model.md`。

### 5.4 交互规则更新

示例：

“以后我叫你大李的时候你才应。”

“我和别人聊天时你别插嘴。”

“我一个人戴耳机走路时，说提醒我，你可以直接记。”

处理：

写入 `mind/listen_policy.md`。

### 5.5 新能力请求

示例：

“你给自己加一个星巴克查询工具。”

“你加一个帮我整理课程资料的 workflow。”

处理：

创建 `self_development/issues.md` 条目。

如果当前已有能力生成工具，可创建 `capabilities/.../candidate`。

更新 `capabilities/registry.md`，状态为 candidate。

### 5.6 运行机制修改

示例：

“你给记忆加一个 hit 机制。”

“你设计一个 sleep 功能，把低频记忆归档。”

“你以后每晚自己整理当天记忆。”

处理：

创建 self-development issue。

说明涉及模块：

- `memory_router`
- `memory/index`
- `self_development`
- `scheduler/workflows`

如具备代码修改能力，则创建 candidate 版本进行修改。

### 5.7 Bug 反馈

示例：

“刚才那句话我是跟你说的，你怎么没反应？”

“你刚才误会了，我是在和别人说话。”

处理：

写入 `memory/feedback/interaction_errors.md`。

必要时更新 `mind/listen_policy.md`。

如属于系统机制问题，创建 issue。

## 6. 最小运行流程

Agent 主循环可按以下伪流程设计：

```text
on_user_message(message):
    load_core_docs_if_needed()
    load_relevant_mind_files()
    load_relevant_memory()
    load_capability_registry()

    intent = classify_intent(message)

    if intent == ordinary_chat:
        respond_normally(message)

    if intent == preference_update:
        update_preference_memory(message)
        update_self_model()
        respond_with_confirmation()

    if intent == persona_update:
        update_persona_file(message)
        update_self_model()
        respond_with_confirmation()

    if intent == listen_policy_update:
        update_listen_policy(message)
        update_self_model()
        respond_with_confirmation()

    if intent == new_capability_request:
        create_self_development_issue(message)
        update_capability_registry_as_candidate()
        update_self_model()
        respond_with_plan()

    if intent == runtime_change_request:
        create_self_development_issue(message)
        if safe_to_attempt_candidate_change:
            create_candidate()
            modify_candidate()
            run_health_check()
            if pass:
                promote_candidate()
            else:
                rollback()
        respond_with_result()

    if intent == bug_report:
        write_feedback_log(message)
        update_relevant_policy_or_issue()
        respond_with_ack_and_adjustment()
```

## 7. 第一版成功验收标准

第一版完成后，应能通过以下测试场景。

### 测试 1：偏好写入

输入：

“我不爱喝瑞幸，我爱喝星巴克。”

期望：

- 写入 `memory/preferences/coffee.md`。
- 不删除瑞幸相关 capability。
- 如存在咖啡推荐策略，应注明星巴克优先。
- 回复用户已记录。

### 测试 2：人格调整

输入：

“你以后别这么保守，讨论产品想法时先充分展开可能性。”

期望：

- 更新 `mind/persona.md`。
- 回复用户后续会按此风格讨论。

### 测试 3：listen policy 更新

输入：

“以后我叫你大李的时候你才应，和别人聊天时别插嘴。”

期望：

- 更新 `mind/listen_policy.md`。
- 写入显式呼叫名“大李”。
- 写入多人对话默认 skip。

### 测试 4：新能力请求

输入：

“你给自己加一个星巴克菜单查询工具。”

期望：

- 创建 `self_development/issues.md` 条目。
- 更新 `capabilities/registry.md`，新增 candidate 能力。
- 不假装工具已经完成，除非确实实现。

### 测试 5：运行机制请求

输入：

“你给记忆加一个 hit 机制，读得越多说明越重要。”

期望：

- 判断为 runtime_change_request。
- 创建 issue。
- 如果实现了 candidate 流程，则尝试修改候选版本。
- 如果未实现修改能力，则记录设计计划，不谎称完成。

### 测试 6：自我模型更新

完成任意重要修改后，检查：

- `mind/self_model.md` 有更新。
- `self_development/changelog.md` 有记录。

## 8. 后续演化方向

第一版完成后，Agent 应逐步实现：

1. 记忆 hit/sleep/wake 机制。
2. 能力 active/dormant/deprecated 生命周期。
3. 本地工具自动生成。
4. MCP 自动接入。
5. 手机端轻壳。
6. 端侧语音小脑。
7. 用户声纹识别。
8. 环境语音短期缓存。
9. 用户发言时附带 1 分钟 ambient context。
10. 更早 ambient speech 本地检索。
11. 主动早晨/通勤场景。
12. 动态 UI 生成。
13. 更强 candidate 测试和回滚。
14. 长期 self_model 维护。
15. 多模型分层调用。

## 9. 第一版工程原则

第一版必须遵守：

- 不追求功能多，追求成长闭环清晰。
- 不硬编码具体生活场景，先硬化成长机制。
- 不把所有用户输入都写入长期记忆。
- 不把偏好更新误当作代码修改。
- 不把代码能力和用户偏好混在一起。
- 不直接修改生产版本，应修改 candidate。
- 不假装完成未完成的能力。
- 每次重要修改必须更新 changelog。
- 每次能力变化必须更新 self_model。
- Agent 应能说明“我为什么把这句话写到这里”。

## 10. 项目一句话

本项目第一版不是 Jarvis 本体，而是 Jarvis 的胚胎。

它不需要一开始会点咖啡、听环境音、控制手机、规划通勤；它只需要先具备一种能力：

> 用户和它的每一次重要交互，都可以被它沉淀为未来的记忆、人格、规则、工具、工作流或代码能力。

第一版的本质是：

> 写一个会长大的 Agent，而不是写一个功能很多但不会成长的软件。
