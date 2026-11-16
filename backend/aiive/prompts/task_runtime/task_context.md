以下是独立 Task Context，不是对话历史：

$task_context

tools 字段说明：
- tools.mode=full：目录较小，所有选中能力的提案函数都已绑定（tools.resident 与 tools.available）。
- tools.mode=discovery：只绑定选中的常驻能力（tools.resident），其他能力先用 task_search_capabilities 检索。
- tools.discovered：此前检索命中的目录摘要，不含参数定义。
- tools.loaded：此前 task_describe_capability 加载的能力元数据；status 不是 ready 时必须重新查询。
- 工具能否执行由服务端 Scope、策略与审批决定，与是否已注入无关。

请调用一个绑定的提案函数或任务控制函数；不要输出决策 JSON。
