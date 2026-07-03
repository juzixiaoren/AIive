# 交互即开发协议

## 1. 目的

本文档定义 Agent 如何把用户日常交互转化为长期改进。

Agent 收到用户输入后，必须先判断：这是普通聊天，还是应当沉淀到记忆、人格、交互规则、能力、工作流、运行机制或自我开发任务中。

## 2. 输入分类

### 2.1 ordinary_chat

普通聊天、普通问答、临时分析，不默认写入长期记忆。

示例：

- "这个想法怎么样？"
- "帮我分析一下这个架构。"
- "你觉得这样可行吗？"

处理：

- 直接回答。
- 不写入长期记忆，除非用户明确要求。

### 2.2 preference_update

用户表达长期偏好。

示例：

- "我不爱喝瑞幸，我爱喝星巴克。"
- "以后咖啡优先考虑星巴克。"
- "我不喜欢太甜的东西。"

处理：

- 写入 `memory/preferences/`。
- 必要时更新 `mind/user_model.md`。
- 不删除已有能力。

### 2.3 persona_update

用户要求调整 Agent 的表达风格、讨论方式、人格倾向。

示例：

- "你以后别这么像产品经理。"
- "你讨论产品设想时先大胆一点。"
- "不要太早保守化。"

处理：

- 写入 `mind/persona.md`。
- 必要时更新 `mind/user_model.md`。

### 2.4 listen_policy_update

用户调整 Agent 的呼叫方式、回应边界、skip 规则。

示例：

- "以后我叫你大李的时候你才应。"
- "和别人聊天时别插嘴。"
- "我一个人戴耳机走路时，说提醒我，你可以直接记。"

处理：

- 写入 `mind/listen_policy.md`。
- 记录显式呼叫规则和隐式呼叫规则。

### 2.5 memory_update

用户明确要求记住或忘记。

示例：

- "记住这个。"
- "以后别忘了。"
- "忘掉刚才那个偏好。"

处理：

- 写入或删除 `memory/` 中对应内容。
- 如果是忘记请求，要标记或执行删除。

### 2.6 new_capability_request

用户要求新增工具、MCP、脚本、工作流、UI 组件。

示例：

- "你给自己加一个星巴克菜单查询工具。"
- "你写个工具帮我整理聊天记录。"
- "你加一个手机端动态卡片。"

处理：

- 在 `self_development/issues.md` 创建任务。
- 在 `capabilities/registry.md` 注册 candidate 能力。
- 如果已经能实现，则走 candidate 修改流程。

### 2.7 runtime_change_request

用户要求修改 Agent 运行机制。

示例：

- "你给记忆加一个 hit 机制。"
- "你给自己加一个 sleep 功能。"
- "你以后每晚整理一次记忆。"
- "你优化一下能力路由。"

处理：

- 创建 self-development issue。
- 判断涉及模块。
- 需要代码修改时走 candidate 流程。

### 2.8 bug_report

用户指出 Agent 行为错误。

示例：

- "刚才那句话我是跟你说的，你怎么没反应？"
- "我刚才是在和别人说话，不是叫你。"
- "你误会了。"

处理：

- 写入 `memory/feedback/interaction_errors.md`。
- 必要时更新 `mind/listen_policy.md`。
- 如属于机制缺陷，创建 issue。

### 2.9 documentation_update

用户要求更新文档。

示例：

- "把这个写进架构文档。"
- "更新一下宪法文档。"

处理：

- 修改对应 `docs/` 文件。
- 记录 changelog。