# 工作流

本目录用于存储工作流。

## 工作流类型

- 自我改进工作流
- 记忆整理工作流
- 能力管理工作流
- 版本管理工作流

## 工作流格式

每个工作流建议包含：

```text
## workflow_name

- Status: active / dormant / deprecated / candidate
- Type: workflow
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

## 示例

### self_improvement_workflow

```text
## self_improvement_workflow

- Status: active
- Type: workflow
- Description: 自我改进工作流
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 用户提出新能力请求、运行机制修改请求时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: docs/self_improvement_protocol.md
- Notes: 基础工作流
```