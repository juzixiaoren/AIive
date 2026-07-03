# 记忆索引

本目录用于存储记忆索引。

## 索引类型

第一版可为空。后续用于：

- hit统计
- sleep/wake机制
- 向量索引
- 全文索引

## 文件格式

索引文件格式待定义。

## 后续计划

1. 实现记忆hit机制
2. 实现记忆sleep/wake机制
3. 实现向量索引
4. 实现全文索引

## 示例

### hit_count.md

```text
# 记忆命中统计

## memory/preferences/coffee.md
- Hit Count: 0
- Last Hit: 
- Status: active

## memory/feedback/interaction_errors.md
- Hit Count: 0
- Last Hit: 
- Status: active
```