# 事件记忆

本目录用于存储用户明确要求记录的事件。

## 记忆类型

- 用户明确要求记住的事件
- 重要生活事件
- 工作相关事件
- 学习相关事件

## 文件格式

每个事件文件建议包含：

```text
## YYYY-MM-DD HH:mm

### Source
用户原话或事件来源。

### Content
事件内容。

### Type
event

### Scope
适用场景。

### Confidence
high / medium / low

### Notes
备注。
```

## 示例

### user_requested_memory.md

```text
## 2026-07-02 18:51

### Source
"记住这个。"

### Content
用户要求记住的内容。

### Type
event

### Scope
用户明确要求记住

### Confidence
high

### Notes
用户明确要求记住。
```