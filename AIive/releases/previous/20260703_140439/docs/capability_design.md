# 能力设计文档

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

能力表示"我能做什么"。

偏好表示"用户希望我什么时候用、如何用、是否主动用"。

用户说"不爱瑞幸"时，不删除瑞幸能力，只改变唤醒条件。

## 6. 能力膨胀管理

当能力过多时，需要：

- hit 统计。
- sleep/dormant。
- 合并重复能力。
- 删除无效 candidate。
- 避免全部能力进入上下文。