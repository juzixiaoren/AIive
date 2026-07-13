# AIive project_plan_v8.md：safe_delete 与 Scope Registry

> 上位文档：`final_project_plan_v7.md`  
> 编码约束：`编码规范.md`  
> 阶段编号：V8  
> 本阶段定位：在不反复打断用户确认的前提下，让 AIive 具备安全删除和未来维护自身存储的基础。

---

## 1. 从上一阶段到本阶段的过渡

从 V7 到 V8：已有工具安全声明，本阶段才允许实现删除工具。删除仍要自由，尤其允许 Agent 管理自身数据，但必须全部经过 safe_delete。

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

用户可以让 AIive 删除测试文件；系统拒绝根目录、home、repo root 等危险路径。

AIive 的差异化要求：本阶段实现不能退化成普通 chatbot / 普通 RAG / 普通工具调用器。所有新增能力都必须服务于长期个人管家目标：理解用户、维护节奏、维护软件身体、给自己加能力。

---

## 3. 必须实现

- 实现 tools/safe_delete.py。
- 实现 SafeDeleteScopeRegistry。
- 实现 safe_delete(path, scope_id, mode)。
- 支持 trash、quarantine、hard_delete_for_test_only。
- 记录 delete_request event。
- 拒绝 /、home root、repo root、active slot root、未解析路径、scope 外路径、符号链接逃逸。


---

## 4. 明确不得实现

- 绕过 safe_delete 的 rm/shutil.rmtree。
- 真实删除用户重要目录。
- memory GC 自动删除。


---

## 5. 后端实现细节

- Scope registry 至少包含 test_sandbox、test_artifacts、inactive_slot_placeholder。
- 删除前必须 realpath 解析。
- 所有删除结果结构化返回 decision/reason/resolved_path。


---

## 6. 前端实现细节

- Chat 触发删除时展示 delete decision。


---

## 7. 数据库 / 存储变更

- delete_requests 可作为 events payload，后续独立表。


---

## 8. API 契约

- POST /api/tools/safe-delete。


---

## 9. 可观测结果

用户可验证：

```text
安全路径可删除/隔离。
```

AI 可验证：

```text
pytest tests/unit/backend/test_safe_delete.py -q；python scripts/smoke/smoke_safe_delete_sandbox.py
```

---

## 10. 验收标准 Done Definition

- 安全路径可删除/隔离。
- 危险路径全部 deny。
- 删除不需用户确认但有记录。
- safe_delete 成为唯一删除入口。


---

## 11. 测试要求

只允许运行本阶段相关测试：

- pytest tests/unit/backend/test_safe_delete.py -q


Smoke 脚本：

- python scripts/smoke/smoke_safe_delete_sandbox.py


禁止默认运行：

```bash
pytest
pytest tests
npm test
pnpm test --all
```

---

## 12. 测试清理要求

- 删除测试只对 tmp_path 副本执行。
- tmp_path 由 pytest 自动清理。
- 不得手动 rm 测试残留。


所有清理必须由测试脚本、fixture 或 cleanup helper 自动完成，不允许编码 AI 测试后手动删除。

---

## 13. 阶段完成后必须更新

```text
docs/current_phase.md
tests/artifacts/v8/<run_id>/summary.json
tests/artifacts/v8/<run_id>/commands.txt
tests/artifacts/v8/<run_id>/cleanup_report.json
```

`docs/current_phase.md` 必须记录：

```text
Phase: V8 safe_delete 与 Scope Registry
Status: completed
Implemented: ...
Not Implemented: ...
Observable Result: ...
Tests Run: ...
Cleanup Result: ...
Known Gaps: ...
Next Phase: V9 MCP Discovery v0：搜索与候选提案，不安装
```


