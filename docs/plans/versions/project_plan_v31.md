# AIive project_plan_v31.md：Observability & Control UI v3：长期个人管家的可解释控制台

> 前置要求：V30 完成。  
> 本阶段目标：把 Chat、任务、记忆、能力、自进化、MCP、维护、watcher 全部接入可解释 UI。  
> 重点：UI 读真实后端 API，不得静态假数据。

---

## 1. 技术选型

```text
React + Vite + TypeScript
TanStack Query
React Router 或 TanStack Router
FastAPI JSON API
```

---

## 2. 必须页面

```text
Chat Page with action cards
Task Dashboard
Notification Inbox
Memory Dashboard
Capability Dashboard
MCP Dashboard
SelfDev Dashboard
Maintenance Dashboard
Watcher Dashboard
Trace Inspector
Context Inspector
Retrieval Inspector
```

---

## 3. 禁止

```text
禁止 UI 自造 truth state。
禁止前端 setTimeout 代替 worker。
禁止 UI 绕过 service 写数据库。
```
