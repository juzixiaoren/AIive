# AIive project_plan_v10.md：MCP Sandbox Install v0：测试服务器、只读工具与能力激活

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V10  
> 本阶段定位：验证 AIive 能给自己加外部能力，但采用 sandbox、只读、可回滚的低风险路径。

---

## 1. 从上一阶段到本阶段的过渡

从 V9 到 V10：已有 MCP candidate，本阶段只允许安装/连接受控测试 MCP 或明确只读低风险 MCP，并在 sandbox 中 smoke 调用。通过后才进入 active capability。

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

用户能看到 AIive 安装了一个测试 MCP，并成功调用只读工具。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 mcp/installer.py。
- 实现 mcp/runtime_client.py。
- 支持 local test MCP server fixture。
- 安装产物写入 capabilities active/sandbox 状态。
- 记录 descriptor_hash、tool_list_hash、smoke_result。
- 只允许 read-only / low risk 工具进入 active。


---

## 4. 明确不得实现

- 安装高风险未知 MCP。
- 允许 MCP 访问 secrets。
- 允许 MCP 删除/写文件/外发。
- 不经 smoke 直接 active。


---

## 5. 后端实现细节

- MCP tool 输出仍是 untrusted tool_result，不能成为 instruction。
- 若 descriptor_hash 变化，capability 状态降级为 needs_review。
- MCP server 配置放在本地 capability registry，不写入 Stable Prefix。


---

## 6. 前端实现细节

- Capabilities 页面显示 MCP active/sandbox/needs_review 状态。


---

## 7. 数据库 / 存储变更

- capabilities
- capability_versions
- mcp_install_records


---

## 8. API 契约

- POST /api/mcp/{candidate_id}/install-sandbox
- POST /api/mcp/{capability_id}/smoke


---

## 9. 可观测结果

用户可验证：

```text
测试 MCP 可安装到 sandbox。
```

AI 可验证：

```text
pytest tests/unit/backend/test_mcp_installer.py -q；pytest tests/unit/backend/test_mcp_runtime_client.py -q；python scripts/smoke/smoke_mcp_test_server.py
```

---

## 10. 验收标准 Done Definition

- 测试 MCP 可安装到 sandbox。
- 只读 tool 可 smoke 调用。
- 通过后 capability active。
- hash 变化会触发 needs_review。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_mcp_installer.py -q
- pytest tests/unit/backend/test_mcp_runtime_client.py -q


Smoke 脚本：

- python scripts/smoke/smoke_mcp_test_server.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 测试 MCP server 用临时进程/fixture，测试结束自动关闭。
- 安装记录带 test_run_id 自动清理。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v10/<run_id>/summary.json
tests/artifacts/v10/<run_id>/commands.txt
tests/artifacts/v10/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V10 MCP Sandbox Install v0：测试服务器、只读工具与能力激活
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V11 Supervisor 与 manifest-based A/B Slot
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

