# 反馈记忆

本目录用于存储用户对Agent行为的反馈。

## 记忆类型

- 误触发
- 漏响应
- 表达风格不合适
- 推荐不合适
- 功能行为错误

## 文件格式

每个反馈文件建议包含：

```text
## YYYY-MM-DD HH:mm

### Source
用户原话或事件来源。

### Content
反馈内容。

### Type
feedback

### Scope
适用场景。

### Confidence
high / medium / low

### Notes
备注。
```

## 示例

### interaction_errors.md

```text
## 2026-07-02 18:51

### Source
"刚才那句话我是跟你说的，你怎么没反应？"

### Content
用户认为Agent应该回应但没有回应。

### Type
feedback

### Scope
交互响应

### Confidence
high

### Notes
可能需要更新listen_policy.md。
```