# 本地工具

本目录用于存储本地工具。

## 工具类型

- 文件读写工具
- 数据处理工具
- 系统管理工具
- 开发辅助工具

## 工具格式

每个工具建议包含：

```text
## tool_name

- Status: active / dormant / deprecated / candidate
- Type: local_tool
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

### file_reader.py

```text
## file_reader

- Status: active
- Type: local_tool
- Description: 读取文件内容
- Created At: 2026-07-02
- Last Used: 2026-07-02
- Hit Count: 1
- Wake Conditions: 需要读取文件时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: core/file_store.py
- Notes: 基础工具
```