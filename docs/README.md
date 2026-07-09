# AIive Architecture Dependency Hardening v3

本包修正了此前阶段计划的问题：终局技术选型写在总纲里，但阶段计划没有足够硬地要求编码 AI 落地，导致它可能用硬编码、模拟、内存状态、散装 if/else 代替真实架构。

执行顺序：

1. 先执行 `project_plan_v0-v20_dependency_architecture_audit_and_repair.md`。
2. 完成并生成两个报告：
   - `docs/audit/v0_v20_dependency_architecture_report.md`
   - `docs/audit/v0_v20_dependency_hardening_completion_report.md`
3. 通过后再进入新的 `project_plan_v21.md`。
4. 不再使用旧 V21-V31。

核心原则：

- 总纲里出现的技术选型必须有落地检查。
- 每个能力必须接 Chat / LangGraph / Dispatcher / Service or Tool / Event / Trace / Action Card。
- 不允许生产路径硬编码、sleep、前端定时、fake/demo 数据。
- V20 之后每个阶段都必须写清楚技术选型、禁止项、验收和测试清理。
