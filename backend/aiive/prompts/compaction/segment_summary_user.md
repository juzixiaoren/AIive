待摘要的源对话（已脱敏）：

${source_turns}

可用的工具摘要引用（局部引用，不是真实 ID）：

${tool_lines}

可用的失败引用：

${fail_lines}

输出必须且只能采用以下 JSON 结构：

{
  "goal": "",
  "outcome": "",
  "decisions": [],
  "entities": [],
  "tool_result_summaries": [],
  "failure_explanations": []
}

数组元素结构：

- `decisions`：每项包含 `what`、`why` 和 `by`；其中 `by` 只能是 `user`、`assistant` 或 `tool`
- `entities`：每项包含 `name`、`type` 和 `relation`
- `tool_result_summaries`：每项包含输入中已有的 `item_ref` 和 `result_summary`
- `failure_explanations`：每项包含输入中已有的 `item_ref` 和 `error`

没有对应事实时保留空数组，不要生成空白占位元素。只输出有效 JSON，不要输出 Markdown、代码块或其他文字。
