# 补充宪法文档：交互、记忆、能力与自我改进协议

> 本文件是 `product_vision_and_v1_architecture.md` 与 `constitution.md` 的补充，用于拆分 Claude Code 需要初始化的四类设计文档：交互即开发、记忆设计、能力设计、自我改进协议。实际项目中可以拆成四个文件：
>
> - `docs/interaction_development_protocol.md`
> - `docs/memory_design.md`
> - `docs/capability_design.md`
> - `docs/self_improvement_protocol.md`

---

# Part 1. interaction_development_protocol.md

## 1. 目的

本文档定义 Agent 如何把用户日常交互转化为长期改进。

Agent 收到用户输入后，必须先判断：这是普通聊天，还是应当沉淀到记忆、人格、交互规则、能力、工作流、运行机制或自我开发任务中。

## 2. 输入分类

### 2.1 ordinary_chat

普通聊天、普通问答、临时分析，不默认写入长期记忆。

示例：

- “这个想法怎么样？”
- “帮我分析一下这个架构。”
- “你觉得这样可行吗？”

处理：

- 直接回答。
- 不写入长期记忆，除非用户明确要求。

### 2.2 preference_update

用户表达长期偏好。

示例：

- “我不爱喝瑞幸，我爱喝星巴克。”
- “以后咖啡优先考虑星巴克。”
- “我不喜欢太甜的东西。”

处理：

- 写入 `memory/preferences/`。
- 必要时更新 `mind/user_model.md`。
- 不删除已有能力。

### 2.3 persona_update

用户要求调整 Agent 的表达风格、讨论方式、人格倾向。

示例：

- “你以后别这么像产品经理。”
- “你讨论产品设想时先大胆一点。”
- “不要太早保守化。”

处理：

- 写入 `mind/persona.md`。
- 必要时更新 `mind/user_model.md`。

### 2.4 listen_policy_update

用户调整 Agent 的呼叫方式、回应边界、skip 规则。

示例：

- “以后我叫你大李的时候你才应。”
- “和别人聊天时别插嘴。”
- “我一个人戴耳机走路时，说提醒我，你可以直接记。”

处理：

- 写入 `mind/listen_policy.md`。
- 记录显式呼叫规则和隐式呼叫规则。

### 2.5 memory_update

用户明确要求记住或忘记。

示例：

- “记住这个。”
- “以后别忘了。”
- “忘掉刚才那个偏好。”

处理：

- 写入或删除 `memory/` 中对应内容。
- 如果是忘记请求，要标记或执行删除。

### 2.6 new_capability_request

用户要求新增工具、MCP、脚本、工作流、UI 组件。

示例：

- “你给自己加一个星巴克菜单查询工具。”
- “你写个工具帮我整理聊天记录。”
- “你加一个手机端动态卡片。”

处理：

- 在 `self_development/issues.md` 创建任务。
- 在 `capabilities/registry.md` 注册 candidate 能力。
- 如果已经能实现，则走 candidate 修改流程。

### 2.7 runtime_change_request

用户要求修改 Agent 运行机制。

示例：

- “你给记忆加一个 hit 机制。”
- “你给自己加一个 sleep 功能。”
- “你以后每晚整理一次记忆。”
- “你优化一下能力路由。”

处理：

- 创建 self-development issue。
- 判断涉及模块。
- 需要代码修改时走 candidate 流程。

### 2.8 bug_report

用户指出 Agent 行为错误。

示例：

- “刚才那句话我是跟你说的，你怎么没反应？”
- “我刚才是在和别人说话，不是叫你。”
- “你误会了。”

处理：

- 写入 `memory/feedback/interaction_errors.md`。
- 必要时更新 `mind/listen_policy.md`。
- 如属于机制缺陷，创建 issue。

### 2.9 documentation_update

用户要求更新文档。

示例：

- “把这个写进架构文档。”
- “更新一下宪法文档。”

处理：

- 修改对应 `docs/` 文件。
- 记录 changelog。

---

# Part 2. memory_design.md

## 1. 目的

记忆系统用于保存用户偏好、反馈、事件、总结和 Agent 自我成长相关信息。

第一版不要求复杂向量数据库，优先实现可解释、可读写的 Markdown 文件。

## 2. 目录结构

```text
memory/
├── preferences/
├── feedback/
├── events/
├── summaries/
├── archive/
└── index/
```

## 3. 记忆类型

### 3.1 preferences

保存长期偏好。

示例：

- 咖啡偏好。
- 交互风格偏好。
- 工具使用偏好。
- 推荐偏好。

### 3.2 feedback

保存用户对 Agent 行为的反馈。

示例：

- 误触发。
- 漏响应。
- 表达风格不合适。
- 推荐不合适。
- 功能行为错误。

### 3.3 events

保存用户明确要求记录的事件。

### 3.4 summaries

保存定期总结。

### 3.5 archive

保存低频、过期但可能仍有用的记忆。

### 3.6 index

第一版可为空。后续用于 hit、sleep、wake、向量索引或全文索引。

## 4. 记忆写入格式建议

每条重要记忆建议包含：

```text
## YYYY-MM-DD HH:mm

### Source
用户原话或事件来源。

### Content
沉淀后的记忆内容。

### Type
preference / feedback / event / rule / summary

### Scope
适用场景。

### Confidence
high / medium / low

### Notes
备注。
```

## 5. hit/sleep/wake 后续目标

### hit

记忆被读取一次，命中次数增加。

### sleep

长期不使用的记忆进入休眠，不主动进入上下文。

### wake

当新上下文与休眠记忆相关时，重新唤醒。

### archive

低价值历史记忆归档。

### forget

用户明确要求忘记时，删除或标记遗忘。

---

# Part 3. capability_design.md

## 1. 目的

能力系统用于记录 Agent 能做什么。

能力可以是：

- 本地工具。
- MCP。
- 工作流。
- UI 组件。
- 记忆维护机制。
- 外部 API。
- 脚本。
- 手机端功能。

## 2. 目录结构

```text
capabilities/
├── registry.md
├── local_tools/
├── workflows/
├── mcp/
├── ui_components/
├── dormant/
└── deprecated/
```

## 3. 能力状态

### active

当前可用，并可参与能力路由。

### dormant

能力存在，但默认不主动唤醒。

例如用户不爱喝瑞幸，则瑞幸工具不删除，但可进入 dormant。

### deprecated

不推荐使用，保留历史记录。

### candidate

用户提出或 Agent 规划中的候选能力，尚未完成。

## 4. registry.md 格式建议

每个能力条目建议包含：

```text
## capability_name

- Status: active / dormant / deprecated / candidate
- Type: local_tool / workflow / mcp / ui_component / runtime
- Description:
- Created At:
- Last Used:
- Hit Count:
- Wake Conditions:
- Suppression Conditions:
- User Preference Links:
- Implementation Path:
- Notes:
```

## 5. 能力与偏好分离

能力表示“我能做什么”。

偏好表示“用户希望我什么时候用、如何用、是否主动用”。

用户说“不爱瑞幸”时，不删除瑞幸能力，只改变唤醒条件。

## 6. 能力膨胀管理

当能力过多时，需要：

- hit 统计。
- sleep/dormant。
- 合并重复能力。
- 删除无效 candidate。
- 避免全部能力进入上下文。

---

# Part 4. self_improvement_protocol.md

## 1. 目的

自我改进协议定义 Agent 如何从用户需求、系统错误、重复任务中生成自我开发任务，并在 candidate 版本中实现、测试、上线或回滚。

## 2. 自我改进流程

```text
用户需求 / 系统反馈 / 重复任务
↓
分类：偏好、人格、规则、能力、运行机制、文档
↓
如果是简单文本沉淀：直接修改 mind/memory/docs
↓
如果是能力或机制变更：创建 issue
↓
必要时创建 candidate
↓
在 candidate 中修改
↓
运行 health check
↓
通过则 promote
↓
失败则 rollback
↓
记录 changelog
↓
更新 self_model
```

## 3. issue 格式建议

```text
## [Open] Issue Title

- Created At:
- Source:
- User Request:
- Type: capability / runtime / memory / listen_policy / bug / docs
- Related Files:
- Proposed Plan:
- Priority:
- Status:
- Notes:
```

## 4. changelog 格式建议

```text
## YYYY-MM-DD HH:mm

### Change
做了什么。

### Reason
为什么做。

### Files
修改了哪些文件。

### Result
成功 / 失败 / 记录为待开发。

### Next
后续动作。
```

## 5. candidate 修改规则

Agent 不应直接修改生产版本核心代码。

流程：

```text
create_candidate()
modify_candidate()
run_health_check()
if pass:
    promote_candidate()
else:
    rollback()
```

## 6. health check 最小要求

必须检查：

- 必要目录存在。
- 必要文件存在。
- Markdown 文件可读写。
- registry 存在。
- mind 文件存在。
- self_development 文件存在。
- logs 可写。
- 主程序可启动。

## 7. promote 规则

只有 candidate 通过 health check，才能 promote。

promote 后：

- 更新 changelog。
- 更新 self_model。
- 保留 previous。
- 记录版本信息。

## 8. rollback 规则

如果 candidate 失败：

- 不覆盖 current。
- 记录失败原因。
- 创建或更新 issue。
- 回复用户说明未完成。

## 9. 诚实原则

如果只是记录 issue，就说“已记录为待开发任务”。

如果只是注册 candidate capability，就说“已注册为候选能力，尚未实现”。

如果通过测试并上线，才说“已实现并生效”。
