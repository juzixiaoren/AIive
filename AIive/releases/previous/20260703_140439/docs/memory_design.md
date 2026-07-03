# 记忆设计文档

## 1. 目的

记忆系统用于保存用户偏好、反馈、事件、总结和 Agent 自我成长相关信息。

第一版不要求复杂向量数据库，优先实现可解释、可读写的 Markdown 文件。

## 2. 目录结构

```text
memory/
├── preferences/
├── feedback/
├── events/
├── summaries/
├── archive/
└── index/
```

## 3. 记忆类型

### 3.1 preferences

保存长期偏好。

示例：

- 咖啡偏好。
- 交互风格偏好。
- 工具使用偏好。
- 推荐偏好。

### 3.2 feedback

保存用户对 Agent 行为的反馈。

示例：

- 误触发。
- 漏响应。
- 表达风格不合适。
- 推荐不合适。
- 功能行为错误。

### 3.3 events

保存用户明确要求记录的事件。

### 3.4 summaries

保存定期总结。

### 3.5 archive

保存低频、过期但可能仍有用的记忆。

### 3.6 index

第一版可为空。后续用于 hit、sleep、wake、向量索引或全文索引。

## 4. 记忆写入格式建议

每条重要记忆建议包含：

```text
## YYYY-MM-DD HH:mm

### Source
用户原话或事件来源。

### Content
沉淀后的记忆内容。

### Type
preference / feedback / event / rule / summary

### Scope
适用场景。

### Confidence
high / medium / low

### Notes
备注。
```

## 5. hit/sleep/wake 后续目标

### hit

记忆被读取一次，命中次数增加。

### sleep

长期不使用的记忆进入休眠，不主动进入上下文。

### wake

当新上下文与休眠记忆相关时，重新唤醒。

### archive

低价值历史记忆归档。

### forget

用户明确要求忘记时，删除或标记遗忘。