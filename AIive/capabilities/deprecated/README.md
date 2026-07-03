# 废弃能力

本目录用于存储废弃的能力。

## 废弃能力类型

- 不再推荐使用的能力
- 已被新能力替代的能力
- 存在严重问题的能力

## 废弃能力格式

每个废弃能力建议包含：

```text
## capability_name

- Status: deprecated
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

## 示例

### old_tool

```text
## old_tool

- Status: deprecated
- Type: local_tool
- Description: 旧工具
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 无
- Suppression Conditions: 已被新工具替代
- User Preference Links: 无
- Implementation Path: 
- Notes: 已被新工具替代
```