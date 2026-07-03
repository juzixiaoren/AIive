# 补丁

本目录用于存储代码补丁。

## 补丁类型

- 功能补丁
- 修复补丁
- 性能补丁
- 安全补丁

## 补丁格式

每个补丁建议包含：

```text
## patch_name

- Created At:
- Source:
- Description:
- Target Files:
- Changes:
- Status: planned / in_progress / applied / failed
- Notes:
```

## 示例

### memory_hit_patch

```text
## memory_hit_patch

- Created At: 2026-07-02
- Source: 用户需求
- Description: 实现记忆hit机制的补丁
- Target Files: core/memory_router.py, memory/index/
- Changes: 添加hit统计功能
- Status: planned
- Notes: 记忆运行机制修改请求
```