# AIive project_plan_v29.md：Maintenance Plane v2：记忆、能力、知识库、事件日志代谢

> 前置要求：V28 完成。  
> 本阶段目标：AIive 能维护自己的记忆、能力、知识库和事件日志，避免无限增长。  
> 重点：维护不是直接 hard delete，而是 sleep/archive/compact/forget/GC 分层。

---

## 1. 必须实现

```text
maintenance planner
auto_safe_actions
risky_actions_needs_review
memory consolidation
capability maintenance
knowledge index maintenance
event archive hot/warm/cold
object store GC
context summary compaction
```

---

## 2. 禁止

```text
禁止自动 hard delete pinned/user_profile/policy/agent_self lesson。
禁止 maintenance 绕过 safe_delete。
禁止删除 lineage 无法解释。
```

---

## 3. 验收

```text
生成 maintenance report。
低 hit episodic memory 可 sleep。
orphan chunks/object refs 可清理。
能力高失败率可 needs_review。
```
