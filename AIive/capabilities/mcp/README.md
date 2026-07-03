# MCP (Model Context Protocol)

本目录用于存储MCP相关能力。

## MCP类型

- 外部API接入
- 数据库连接
- 文件系统访问
- 网络服务

## MCP格式

每个MCP建议包含：

```text
## mcp_name

- Status: active / dormant / deprecated / candidate
- Type: mcp
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

### external_api_mcp

```text
## external_api_mcp

- Status: candidate
- Type: mcp
- Description: 外部API接入MCP
- Created At: 2026-07-02
- Last Used: 
- Hit Count: 0
- Wake Conditions: 需要调用外部API时
- Suppression Conditions: 无
- User Preference Links: 无
- Implementation Path: 
- Notes: 第一版暂不实现
```