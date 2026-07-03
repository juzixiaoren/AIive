# UI 组件

本目录用于存储UI组件。

## UI组件类型

- CLI界面
- Web界面
- 手机端界面
- 桌面端界面

## UI组件格式

每个UI组件建议包含：

```text
## ui_component_name

- Status: active / dormant / deprecated / candidate
- Type: ui_component
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

### cli_interface

```text
## cli_interface

- Status: active
- Type: ui_component
- Description: CLI交互界面
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 启动时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: main.py
- Notes: 第一版CLI界面
```