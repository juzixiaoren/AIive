# AIive project_plan_v7.md：Tool Registry、Capability Safety 与 Permission Manager

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V7  
> 本阶段定位：让 Agent 的行动能力从一开始就在 Kernel 可检查边界内，而不是让 LLM 直接执行。

---

## 1. 从上一阶段到本阶段的过渡

从 V6 到 V7：AIive 已能记住用户与节奏，但还不能行动。本阶段建立工具和能力的机械安全声明，先注册低风险工具。

过渡规则：

```text
1. 必须保留上一阶段已通过的用户可观察结果。
2. 必须优先新增模块、表、API，不做大范围删除。
3. 旧 API 如需扩展，只能兼容扩展，不能破坏返回结构。
4. 本阶段完成前，不得实现后续阶段功能。
5. 如果需要迁移数据，必须提供可重复执行的脚本或 Alembic migration，并在测试库验证。
```

---

## 2. 本阶段产品意义

用户能看到 AIive 有哪些工具、每个工具能做什么、风险等级是什么。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 tools/registry.py。
- 定义 CapabilitySafetySchema。
- 注册 echo、read_text_file_limited 两个低风险工具。
- 实现 permission_manager.py。
- untrusted content 不能触发工具调用。
- require_confirmation 工具返回 approval_required，不执行。


---

## 4. 明确不得实现

- 真实发邮件。
- 真实购买支付。
- 真实删除。
- MCP 安装。


---

## 5. 后端实现细节

- 工具声明必须包含 definition_source、definition_trust_level、risk_level、requires_confirmation、writes_external_world、can_access_secret、can_delete、descriptor_hash。
- tool_description_is_instruction 恒为 false。


---

## 6. 前端实现细节

- Tools 页面可选；至少 API 可查看工具列表。


---

## 7. 数据库 / 存储变更

- capabilities 表可在本阶段创建，或先用静态注册 + 后续迁移。


---

## 8. API 契约

- GET /api/tools
- POST /api/tools/{id}/dry-run 可选。


---

## 9. 可观测结果

用户可验证：

```text
工具列表可见。
```

AI 可验证：

```text
pytest tests/unit/backend/test_tool_registry.py -q；pytest tests/unit/backend/test_permission_manager.py -q；python scripts/smoke/smoke_tool_list.py
```

---

## 10. 验收标准 Done Definition

- 工具列表可见。
- 权限检查机械执行。
- untrusted source 无法触发工具。
- 确认边界可返回 approval_required。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_tool_registry.py -q
- pytest tests/unit/backend/test_permission_manager.py -q


Smoke 脚本：

- python scripts/smoke/smoke_tool_list.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 工具 dry-run 不写真实外部世界。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v7/<run_id>/summary.json
tests/artifacts/v7/<run_id>/commands.txt
tests/artifacts/v7/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V7 Tool Registry、Capability Safety 与 Permission Manager
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V8 safe_delete 与 Scope Registry
```


---

## 成熟实践参照

本阶段设计遵循以下成熟实践，但不直接把复杂框架整体搬进来：

```text
LangGraph / LlamaIndex：短期线程记忆 + 长期记忆 + token 限制 + 检索注入。
OpenAI Agents Guardrails：工具调用前后进行机械 guardrail，而不是只相信 LLM。
MCP 官方 Registry / MCP Security：MCP 先发现、评估、沙箱验证，再启用；防 tool poisoning / rug pull。
Blue-Green / Rolling Update：A/B slot、健康检查、失败回滚。
Transactional Outbox：业务状态、事件、异步任务同事务写入，由 worker 可靠消费。
Expand-Contract Migration：先兼容扩展，再切换，最后收缩旧结构。
```

