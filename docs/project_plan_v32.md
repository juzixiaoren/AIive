# AIive project_plan_v32.md：Architecture Conformance Checker v1

> 前置要求：V31 完成。  
> 本阶段目标：把这次人工发现的问题转化为可重复运行的架构合规检查。  
> 重点：不做大量人工 eval，做机械 contract checks。

---

## 1. 必须实现

```text
scripts/check_architecture.py
检查：
  /api/chat 是否走 LangGraph
  是否存在 sleep/setTimeout 真实调度
  是否存在绕过 safe_delete 删除
  是否存在 chat handler 直接 tool call
  是否存在 active slot patch
  是否存在 Fake/Demo 进入生产路径
  每个 capability 是否有 schema/permission/action card
  每个 LLM call 是否有 context_snapshot
```

---

## 2. 验收

```text
本脚本能在 CI/本地运行。
失败时输出具体文件和原因。
不能替代单元测试，但可防止架构再次走偏。
```
