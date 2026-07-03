# 休眠能力

本目录用于存储休眠状态的能力。

## 休眠能力类型

- 低频使用的能力
- 用户偏好不使用的能力
- 暂时不需要的能力

## 休眠能力格式

每个休眠能力建议包含：

```text
## capability_name

- Status: dormant
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

### luckin_coffee_tool

```text
## luckin_coffee_tool

- Status: dormant
- Type: local_tool
- Description: 瑞幸咖啡点单工具
- Created At: 2026-07-02
- Last Used: 
- Hit Count: 0
- Wake Conditions: 用户要求使用瑞幸咖啡时
- Suppression Conditions: 用户偏好不使用瑞幸咖啡
- User Preference Links: memory/preferences/coffee.md
- Implementation Path: 
- Notes: 用户不爱喝瑞幸，但能力不删除
```