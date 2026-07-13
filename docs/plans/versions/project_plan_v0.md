# AIive project_plan_v0.md：项目地基、技术栈锁定与健康检查

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V0  
> 本阶段定位：让 AIive 从第一天就不是“散文件实验”，而是可被 AI 编码稳定扩展的工程项目。

---

## 1. 从上一阶段到本阶段的过渡

从空项目进入 V0。此阶段只建立可运行骨架和开发契约，不追求业务能力。完成后项目必须有明确技术栈、目录结构、配置入口、健康检查和测试/清理规范落点。

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

用户可以启动本地后端并访问健康检查，确认项目不是空架构。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 创建 README.md、pyproject.toml、.env.example、docker-compose.yml 占位。
- 创建 docs/current_phase.md、docs/编码规范.md、docs/adr/0001-tech-stack.md。
- 创建 backend/aiive/main.py、config.py、api/routes_health.py。
- 创建 tests/unit/backend/test_health.py 与 tests/artifacts/.gitkeep。
- GET /health 返回 ok、service、version 三个字段。


---

## 4. 明确不得实现

- 接入真实 LLM。
- 实现 memory、tool、safe_delete、MCP、self-dev。
- 引入 Flask/Django/SQLite-only。
- 创建复杂前端页面。


---

## 5. 后端实现细节

- FastAPI app factory：create_app()。
- pydantic-settings 读取 .env，但无 secret 输出。
- 健康检查不依赖数据库。


---

## 6. 前端实现细节

- 仅创建 frontend 目录和 package.json 占位，可暂不启动 UI。


---

## 7. 数据库 / 存储变更

- 本阶段无业务表。


---

## 8. API 契约

- GET /health。


---

## 9. 可观测结果

用户可验证：

```text
后端能启动。
```

AI 可验证：

```text
pytest tests/unit/backend/test_health.py -q
```

---

## 10. 验收标准 Done Definition

- 后端能启动。
- /health 返回 ok。
- current_phase.md 标记 V0 completed。
- 没有未来阶段业务代码。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_health.py -q


Smoke 脚本：

- 本阶段无真实外部 smoke，或仅手动 UI 验证。


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- health 测试不得写文件/数据库。
- tests/artifacts 只保留 .gitkeep。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v0/<run_id>/summary.json
tests/artifacts/v0/<run_id>/commands.txt
tests/artifacts/v0/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V0 项目地基、技术栈锁定与健康检查
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V1 真实 LLM Client 与 Smoke 验证
```


