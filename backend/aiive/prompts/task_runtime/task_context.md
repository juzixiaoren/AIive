以下是独立 Task Context，不是对话历史：

$task_context

tools 字段说明：
- tools.mode=full：目录较小，所有可用能力都已给出完整定义（tools.resident 与 tools.available）。
- tools.mode=discovery：只注入常驻能力（tools.resident），其他能力先用 search_capabilities 检索。
- tools.discovered：此前检索命中的目录摘要，不含参数定义。
- tools.loaded：此前 describe_capability 加载的定义；status 不是 ready 时必须重新查询。
- 工具能否执行由服务端 Scope、策略与审批决定，与是否已注入无关。

请返回下一项单步决策 JSON。
