# AIive project_plan_v28.md：Personal Steward v2：生活节奏主动维护与可解释建议

> 前置要求：V27 完成。  
> 本阶段目标：AIive 从“能提醒”升级为“越来越懂用户生活节奏的个人管家”。  
> 重点：主动建议必须基于真实 tasks/events/memory/KG，不得编造。

---

## 1. 必须实现

```text
steward signals:
  overdue_tasks
  ignored_reminders
  frequent_topic_switches
  long_idle_projects
  user_declared_deadlines
  routine_patterns
  energy/time preference memories

suggestion levels:
  silent
  chat_badge
  soft_suggestion
  notification
  urgent_local_alert
```

---

## 2. Chat 入口

```text
我最近是不是有什么事情拖着？
你觉得我今天应该先做什么？
别老提醒我这个。
这个项目下次继续时提醒我。
```

---

## 3. 禁止

```text
禁止编造日程。
禁止外发消息。
禁止连续高频打扰。
```
