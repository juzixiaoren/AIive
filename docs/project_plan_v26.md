# AIive project_plan_v26.md：Self-Evolution Loop v3：失败复盘、Lesson Memory、再次修复

> 前置要求：V25 完成。  
> 本阶段目标：AIive 从自我修改失败中学习，而不是重复犯错。  
> 重点：不得无限循环；不得全量测试；不得绕过 inactive slot。

---

## 1. 必须实现

```text
repair budget: max_repair_attempts 默认 2
failure analysis: test_result -> failure_signature/root_cause
lesson memory: agent_self memory，默认 pinned/never hard delete
repair plan: 引用 lesson 再次 patch inactive slot
targeted tests only
needs_user_review card after budget exhausted
```

---

## 2. Chat 入口

```text
刚才失败原因是什么？
根据失败原因再修一次。
不要重复上次那个错误。
```

---

## 3. 验收

```text
失败测试能生成 lesson memory。
再次修复时 Context Builder 注入 lesson。
连续失败停止并显示 needs_user_review。
```
