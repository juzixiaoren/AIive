以下是独立 Task Context，不是对话历史：

$task_context

tools 字段说明：
- tools.mode=full：目录较小，所有选中能力的提案函数都已绑定（tools.resident 与 tools.available）。
- tools.mode=discovery：按授权和预算绑定常驻工具与已加载工具。其他能力可用 task_search_capabilities 检索。
- tools.discovered：最近检索命中的 Namespace 和工具摘要。signals 中的 tool_search 事件提供检索模式、索引状态和加载的能力 ID。
- tools.loaded：搜索自动加载或 task_describe_capability 单独加载的能力。使用当前已绑定的 status=ready 提案函数。
- 工具能否执行由服务端 Scope、策略与审批决定，与是否已注入无关。

请调用一个绑定的提案函数或任务控制函数；不要输出决策 JSON。
