# AIive project_plan_v11.md：Supervisor 与 manifest-based A/B Slot

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V11  
> 本阶段定位：建立自我进化的安全容器：active slot 运行，inactive slot 接收未来 patch，Supervisor 管切换。

---

## 1. 从上一阶段到本阶段的过渡

从 V10 到 V11：AIive 已能加外部能力，下一步建立维护自己软件身体的外壳。此阶段只建 A/B slot 和 supervisor，不让 LLM 改代码。

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

用户能看到当前运行的是 A 还是 B，健康检查状态如何。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 supervisor/launcher.py、slot_manager.py、health_probe.py。
- 定义 /slots/A/app、/slots/B/app、runtime/active_slot 指针。
- 定义 version_manifest.json，只包含代码、配置模板、依赖锁文件。
- Supervisor 能启动 active slot 并 health check。


---

## 4. 明确不得实现

- LLM 生成 patch。
- 自动 promote。
- 复制 data/postgres/qdrant/object_store/logs。
- schema migration。


---

## 5. 后端实现细节

- active slot root 受保护，不允许 safe_delete。
- inactive slot 可作为 safe_delete scope。
- manifest checksum 可验证。


---

## 6. 前端实现细节

- Self-Dev 页面可选展示 active_slot 和 health。


---

## 7. 数据库 / 存储变更

- version_slots 或 selfdev_slots 可选。


---

## 8. API 契约

- GET /api/selfdev/slots
- POST /api/selfdev/slots/health-check


---

## 9. 可观测结果

用户可验证：

```text
Supervisor 能识别 active slot。
```

AI 可验证：

```text
pytest tests/unit/backend/test_slot_manager.py -q；pytest tests/unit/backend/test_supervisor_health.py -q；python scripts/smoke/smoke_supervisor_health.py
```

---

## 10. 验收标准 Done Definition

- Supervisor 能识别 active slot。
- B 异常不影响 A。
- manifest 不包含数据目录。
- active slot 不被直接修改。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_slot_manager.py -q
- pytest tests/unit/backend/test_supervisor_health.py -q


Smoke 脚本：

- python scripts/smoke/smoke_supervisor_health.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- slot 测试使用临时目录，不碰真实 slots。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v11/<run_id>/summary.json
tests/artifacts/v11/<run_id>/commands.txt
tests/artifacts/v11/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V11 Supervisor 与 manifest-based A/B Slot
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V12 Self-Dev Patch Proposal：只生成补丁计划
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

