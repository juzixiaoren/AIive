"""
ReAct Agent模块

实现 ReAct 循环：思考 -> 行动 -> 观察 -> 思考 -> 行动 -> 观察 -> ...

Agent 只有三种操作：
1. chat - 回复用户
2. 调用工具 - 执行具体操作
3. 使用 skill - 调用预定义的技能
"""

import json
from typing import Any, Callable

from core.llm_client import LLMClient
from core.tool_registry import ToolRegistry, get_tool_registry


class ReActAgent:
    """ReAct Agent"""

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry | None = None,
        max_iterations: int = 10,
        message_callback: Callable[[str], None] | None = None
    ) -> None:
        """
        初始化 ReAct Agent

        Args:
            llm_client: LLM 客户端
            tool_registry: 工具注册表
            max_iterations: 最大迭代次数
            message_callback: 消息回调函数，用于发送消息给用户
        """
        self.llm_client = llm_client
        self.tool_registry = tool_registry or get_tool_registry()
        self.max_iterations = max_iterations
        self.message_callback = message_callback or print

    def run(self, user_input: str, context: dict[str, Any] | None = None) -> str:
        """
        运行 ReAct 循环

        Args:
            user_input: 用户输入
            context: 额外上下文

        Returns:
            str: 最终回复
        """
        self.message_callback(f"[Agent] 收到用户请求: {user_input[:50]}...")

        # 初始化对话历史
        messages = self._build_initial_messages(user_input, context)

        # ReAct 循环
        for iteration in range(self.max_iterations):
            self.message_callback(f"[Agent] 迭代 {iteration + 1}/{self.max_iterations}")

            # 1. 思考：让 LLM 决定下一步行动
            self.message_callback("[Agent] 正在思考...")
            llm_response = self._think(messages)

            # 2. 解析 LLM 响应
            action = self._parse_action(llm_response)

            # 3. 如果是最终回复，返回
            if action["type"] == "final_answer":
                self.message_callback("[Agent] 生成最终回复")
                return action["content"]

            # 4. 如果是调用工具
            if action["type"] == "tool_call":
                tool_name = action["tool"]
                tool_args = action.get("args", {})

                self.message_callback(f"[Agent] 调用工具: {tool_name}")

                # 5. 执行工具
                tool_result = self.tool_registry.execute_tool(tool_name, **tool_args)

                self.message_callback(f"[Agent] 工具执行结果: {'成功' if tool_result.get('success') else '失败'}")

                # 6. 观察：将工具结果添加到对话历史
                messages.append({
                    "role": "assistant",
                    "content": llm_response
                })
                messages.append({
                    "role": "user",
                    "content": f"工具执行结果:\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}\n\n请继续思考下一步行动。"
                })

                continue

            # 7. 如果是发送消息给用户
            if action["type"] == "message":
                self.message_callback(f"[Agent] {action['content']}")
                # 继续循环
                messages.append({
                    "role": "assistant",
                    "content": llm_response
                })
                messages.append({
                    "role": "user",
                    "content": "消息已发送给用户，请继续思考下一步行动。"
                })
                continue

            # 8. 如果 LLM 响应无法解析，要求重新生成
            self.message_callback("[Agent] 无法解析 LLM 响应，要求重新生成")
            messages.append({
                "role": "assistant",
                "content": llm_response
            })
            messages.append({
                "role": "user",
                "content": "你的响应格式不正确。请使用以下格式之一：\n1. 调用工具: {\"tool\": \"工具名\", \"args\": {\"参数\": \"值\"}}\n2. 发送消息: {\"message\": \"消息内容\"}\n3. 最终回复: {\"final_answer\": \"回复内容\"}"
            })

        # 达到最大迭代次数
        self.message_callback("[Agent] 达到最大迭代次数，生成最终回复")
        return self._generate_fallback_answer(user_input, messages)

    def _build_initial_messages(
        self,
        user_input: str,
        context: dict[str, Any] | None
    ) -> list[dict[str, str]]:
        """构建初始对话历史"""
        # 系统提示
        system_prompt = self._build_system_prompt()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]

        # 添加额外上下文
        if context:
            context_text = "\n\n## 额外上下文\n"
            for key, value in context.items():
                context_text += f"### {key}\n{value}\n\n"
            messages[1]["content"] += context_text

        return messages

    def _build_system_prompt(self) -> str:
        """构建系统提示"""
        tools_schema = self.tool_registry.get_tools_schema()
        tools_text = json.dumps(tools_schema, ensure_ascii=False, indent=2)

        return f"""你是一个智能助手，通过调用工具来完成任务。

## 可用工具

{tools_text}

## 行动格式

你必须使用以下 JSON 格式之一来响应：

1. **调用工具**：当你需要执行具体操作时
```json
{{"tool": "工具名", "args": {{"参数1": "值1", "参数2": "值2"}}}}
```

2. **发送消息**：当你需要向用户发送进度消息时
```json
{{"message": "消息内容"}}
```

3. **最终回复**：当你完成任务或需要回复用户时
```json
{{"final_answer": "回复内容"}}
```

## 工作流程

1. 分析用户请求
2. 思考需要哪些步骤来完成任务
3. 逐步调用工具执行操作
4. 观察工具执行结果
5. 继续思考下一步行动
6. 完成后生成最终回复

## 重要规则

- 每次只调用一个工具
- 如果工具执行失败，分析原因并尝试修复
- 定期向用户发送进度消息
- 完成任务后必须生成最终回复
- 不要编造工具或参数
"""

    def _think(self, messages: list[dict[str, str]]) -> str:
        """让 LLM 思考"""
        try:
            response = self.llm_client.chat(messages, temperature=0.3)
            return response
        except Exception as e:
            return json.dumps({"final_answer": f"思考过程中发生错误: {str(e)}"})

    def _parse_action(self, llm_response: str) -> dict[str, Any]:
        """解析 LLM 响应"""
        try:
            # 尝试直接解析 JSON
            data = json.loads(llm_response)

            # 检查是否是最终回复
            if "final_answer" in data:
                return {"type": "final_answer", "content": data["final_answer"]}

            # 检查是否是工具调用
            if "tool" in data:
                return {
                    "type": "tool_call",
                    "tool": data["tool"],
                    "args": data.get("args", {})
                }

            # 检查是否是消息
            if "message" in data:
                return {"type": "message", "content": data["message"]}

            # 无法解析
            return {"type": "unknown", "content": llm_response}

        except json.JSONDecodeError:
            # 如果不是 JSON，尝试提取 JSON 块
            import re
            json_match = re.search(r'```json\s*(.*?)\s*```', llm_response, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    if "final_answer" in data:
                        return {"type": "final_answer", "content": data["final_answer"]}
                    if "tool" in data:
                        return {
                            "type": "tool_call",
                            "tool": data["tool"],
                            "args": data.get("args", {})
                        }
                    if "message" in data:
                        return {"type": "message", "content": data["message"]}
                except json.JSONDecodeError:
                    pass

            # 如果无法解析，作为最终回复
            return {"type": "final_answer", "content": llm_response}

    def _generate_fallback_answer(
        self,
        user_input: str,
        messages: list[dict[str, str]]
    ) -> str:
        """生成降级回复"""
        return f"抱歉，我无法完成这个任务。请尝试更具体的请求。"


# 全局实例
_react_agent: ReActAgent | None = None


def get_react_agent() -> ReActAgent:
    """获取全局 ReAct Agent"""
    global _react_agent
    if _react_agent is None:
        from core.llm_client import get_llm_client
        _react_agent = ReActAgent(get_llm_client())
    return _react_agent
