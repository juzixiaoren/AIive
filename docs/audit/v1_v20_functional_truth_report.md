# V1-V20 功能真实性审查报告

> 基于 `AIive_v1-v20_functional_audit_and_repair_report.md` Step 1  
> 执行时间: 2026-07-08 20:10

---

## 1. 能力状态总表

| 能力 | 状态 | Chat可触 | LangGraph | 持久化 | 工具 | trace | 测试 | 等级 |
|------|------|:--:|:--:|:--:|:--:|:--:|:--:|------|
| 连续对话(刷新不丢) | FAKE | ❌ | ❌ | ❌ | N/A | N/A | ❌ | **BLOCKER** |
| /api/chat LangGraph | PARTIAL | ✅ | ⚠️ | ⚠️ | N/A | ✅ | ⚠️ | BLOCKER |
| Reminder 定时提醒 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | MAJOR |
| scheduled_tool_task | MISSING | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | BLOCKER |
| Execution Intent Gate | **MISSING** | ❌ | ❌ | N/A | N/A | N/A | ❌ | **BLOCKER** |
| 改名记忆 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | MAJOR |
| 用户称呼 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | MAJOR |
| safe_delete | PASS | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| Knowledge 摄入 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | MAJOR |
| Knowledge 检索 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | MAJOR |
| MCP 搜索 | PARTIAL | ✅ | ✅ | N/A | ✅ | ⚠️ | ✅ | MAJOR |
| MCP 沙箱 | PARTIAL | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | MAJOR |
| SelfDev Plan | PASS | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| A/B Slot | PASS | ✅ | N/A | ✅ | ✅ | ✅ | ✅ | — |
| Attention State | PASS | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | MINOR |
| Rhythm | PASS | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | MINOR |

---

## 2. BLOCKER 详情

### BLOCKER-1: 单一连续对话缺失（刷新丢对话）

**症状**: ChatPage 的消息存在 React `useState` 中，刷新浏览器全部丢失。每次新打开页面 thread_id 为 undefined，创建新 thread。

**证据**:
- `frontend/src/pages/ChatPage.tsx:13`: `const [messages, setMessages] = useState<Message[]>([]);` — 纯内存状态
- `frontend/src/pages/ChatPage.tsx:15`: `const [threadId, setThreadId] = useState<string | undefined>();` — 初始 undefined
- 前端无 `localStorage` 持久化 thread_id
- 后端 threads 表有历史 thread，但从未恢复

**影响**: 每次刷新即失忆。AIive 不像个人管家，像普通 chatbot。

### BLOCKER-2: Execution Intent Gate 完全缺失

**症状**: 用户问"如果我想让你改名，你会调用哪个工具？"，系统可能触发 `remember_or_update` 副作用。

**证据**:
- `grep "explain_only\|should_execute\|execution_mode" backend/aiive/runtime/` → 0 匹配
- 无任何代码区分"假设性问题"和"真实命令"
- 系统提示词未要求 LLM 区分
- 没有 `blocked_when` 检查

**影响**: 用户问工具选择 → 系统误写记忆。这是"执行 vs 解释"的破坏性 bug。

### BLOCKER-3: scheduled_tool_task 完全缺失

**症状**: 文档要求 `schedule_tool_task` 工具（到期后唤醒 Agent 执行某工具），但完全未实现。

**证据**: `grep "schedule_tool_task" backend/` → 0 匹配

**影响**: 用户说"一分钟后执行 current_time 工具"无法实现。

---

## 3. MAJOR 详情

### MAJOR-1: Reminder daemon 依赖服务启动

**症状**: daemon 已实现（每 10 秒轮询），但仅在 FastAPI lifespan 中启动。如果服务未运行，定时任务永不触发。

**修复方向**: 文档要求 worker 独立于 Web 服务。当前实现耦合在 app 生命周期中，尚可接受但需标注。

### MAJOR-2: 前端无 Notification Inbox 展示

**症状**: GET /api/notifications 存在，但前端无对应 UI 展示通知列表。用户看不到已触发的提醒。

**证据**: `grep "notification\|Notification" frontend/src/pages/` → 仅 EventTimeline.tsx 有 LABEL 映射

### MAJOR-3: Knowledge 摄入缺少 object_ref

**症状**: `ingest_document` 创建 documents/chunks 记录，但未调用 `object_store.put_text()` 保存原始文档。

**证据**: `builtin_tools.py:_handle_ingest_document` 仅调 `KnowledgeIngestor.ingest()`，未调 object_store

### MAJOR-4: MCP 候选为硬编码内置目录

**症状**: `search_mcp_candidates` 返回内置 6 个固定候选，非来自真实 registry/config source。

**证据**: `mcp/discovery.py` `_BUILTIN_CATALOG` 为硬编码列表

---

## 4. 通过标准的逐项对照

| # | 标准 | 状态 |
|---|------|:--:|
| 1 | 刷新后对话不消失 | ❌ FAKE |
| 2 | 无新建对话入口，只有一个 active thread | ⚠️ 无按钮但每次刷新新 thread |
| 3 | /api/chat 进入 LangGraph | ✅ 通过 `invoke_chat()` → `graph.invoke()` |
| 4 | "如果改名调什么工具"不写记忆 | ❌ 无 Gate 保护 |
| 5 | A→B→你叫什么？只回答 B，A superseded | ✅ (修后) |
| 6 | "一分钟后发 hi"真实 task + worker + notification | ✅ (修后) |
| 7 | "一分钟后执行 current_time"到期唤醒 | ❌ MISSING |
| 8 | safe_delete 唯一删除入口 | ✅ |
| 9 | ChatResponse 有 action card | ✅ |
| 10 | trace/event/snapshot 可追踪 | ✅ |
| 11 | 测试自动清理 | ✅ |

---

## 5. 修复顺序

1. **BLOCKER-2: Execution Intent Gate** — 系统提示词加入规则 + 工具侧 `blocked_when` 检查
2. **BLOCKER-1: 单一连续对话** — 前端 localStorage 持久化 thread_id + 刷新恢复消息
3. **MAJOR-1: Reminder daemon 文档标注**
4. **MAJOR-2: 前端 Notification Inbox**
5. **MAJOR-3: Knowledge object_ref**
