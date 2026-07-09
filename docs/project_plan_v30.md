# AIive project_plan_v30.md：Long-running Watchers v2：条件观察、周期检查、主动回到用户

> 前置要求：V29 完成。  
> 本阶段目标：AIive 能长期观察用户指定条件，并在条件满足时主动提醒。  
> 重点：Watcher 是 tasks/outbox/worker 的任务类型，不是独立平台。

---

## 1. 必须实现

```text
watch_id/title/condition_type/condition_expr/check_interval
last_checked_at/next_check_at/status/source_tools/related_memory_ids
condition evaluator:
  time
  local file
  memory/event
  knowledge stale
  low-risk MCP/tool
```

---

## 2. Chat 入口

```text
如果这个文件更新了提醒我。
每天检查一下有没有没完成的提醒。
当你发现自己某个能力连续失败时提醒我。
停止这个 watcher。
```

---

## 3. 禁止

```text
禁止高频无限轮询。
禁止 watcher 自动外发消息。
禁止高风险 tool condition 自动执行。
```
