"""
Decision Engine模块

调用LLM输出结构化决策。
"""

import json
from typing import Dict, Any, List, Optional
from core.llm_client import LLMClient
from core.context_builder import ContextBuilder


# 系统提示词
SYSTEM_PROMPT = """你不是普通聊天助手。你是这个 Agent 的自我开发大脑。

你必须根据用户请求判断应该修改哪些文件，并在 operations 中输出实际的文件操作。你不能声称完成未执行的修改——如果需要记录信息，必须在 operations 中包含对应的文件操作。

你必须区分：
- 普通聊天 (chat)：纯对话，不需要记录任何信息
- 偏好更新 (memory_update)：用户表达偏好、习惯、喜好，必须写入 memory/preferences/ 目录
- 人格更新 (persona_update)：修改 Agent 人格设定，写入 mind/persona.md
- listen policy 更新 (listen_policy_update)：修改监听策略，写入 mind/listen_policy.md
- 能力变更 (capability_change)：新增或修改能力
- 代码修改 (code_change)：修改代码逻辑
- 文档更新 (docs_update)：更新文档
- 自我改进 (self_improvement)：Agent 自我改进请求，创建 issue 或修改代码

关键规则：
1. 如果 request_type 不是 "chat"，operations 不能为空——必须包含实际的文件操作
2. 用户偏好必须保存到 memory/preferences/ 目录下对应的 .md 文件（如咖啡偏好写 memory/preferences/coffee.md）
3. listen policy 更新必须操作 mind/listen_policy.md
4. 代码修改或自我改进请求如果暂时无法实现，必须创建 self_development/issues.md 记录
5. 文件操作只允许阅读和修改，禁止删除操作"""


# 决策输出格式说明
DECISION_SCHEMA = """你必须输出以下JSON格式：

{
  "request_type": "chat | memory_update | persona_update | listen_policy_update | capability_change | code_change | docs_update | self_improvement",
  "confidence": 0.0到1.0之间的浮点数,
  "summary": "用户真正想要什么的简短描述",
  "needs_code_change": true或false,
  "needs_file_update": true或false,
  "target_files": ["需要修改的文件路径列表"],
  "read_more_files": ["如果需要读取更多文件，在此列出"],
  "plan": ["步骤1", "步骤2"],
  "operations": [
    {
      "type": "read_file | write_file | append_file | patch_file | create_file | create_issue | update_registry | run_tests",
      "path": "相对路径",
      "content": "内容或patch",
      "reason": "为什么这么做"
    }
  ],
  "run_tests": true或false,
  "tests_to_run": ["需要运行的测试"],
  "user_response_draft": "给用户的回复草稿"
}

注意：
- 如果 request_type 不是 "chat"，operations 不能为空——必须包含实际的文件操作
- 偏好更新(memory_update)必须在 operations 中包含 append_file 或 write_file 操作
- 文件路径都是相对于项目根目录的
- 禁止任何删除操作
- 如果需要读取更多文件，read_more_files不为空，此时operations应为空，等读取后再生成操作
- run_tests: 是否需要运行测试，默认为true
- tests_to_run: 如果需要运行测试，指定要运行的测试文件或目录，默认运行所有测试"""


class DecisionEngine:
    """决策引擎"""
    
    def __init__(self, llm_client: LLMClient, context_builder: ContextBuilder):
        """
        初始化决策引擎
        
        Args:
            llm_client: LLM客户端
            context_builder: 上下文构建器
        """
        self.llm_client = llm_client
        self.context_builder = context_builder
        self.max_retries = 2
    
    def make_decision(
        self,
        user_request: str,
        session_context: Optional[Dict[str, Any]] = None,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        做出决策
        
        Args:
            user_request: 用户请求
            session_context: 会话上下文（短期记忆）
            additional_context: 额外上下文
            
        Returns:
            dict: 决策结果
        """
        print("[DecisionEngine] 正在构建决策上下文...")
        # 构建上下文
        context_text = self.context_builder.build_decision_context(user_request)
        
        # 添加会话上下文（短期记忆）
        if session_context:
            session_info = self._format_session_context(session_context)
            context_text += "\n\n## 会话上下文（短期记忆）\n" + session_info
        
        # 构建消息
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + DECISION_SCHEMA},
            {"role": "user", "content": context_text}
        ]
        
        # 如果有额外上下文，添加到消息中
        if additional_context:
            extra_info = "\n\n## 额外上下文\n"
            for key, value in additional_context.items():
                extra_info += f"### {key}\n{value}\n\n"
            messages[1]["content"] += extra_info
        
        # 调用LLM
        for attempt in range(self.max_retries):
            print(f"[DecisionEngine] 正在调用LLM API (尝试 {attempt + 1}/{self.max_retries})...")
            try:
                decision = self.llm_client.chat_json(messages, temperature=0.3)
                print("[DecisionEngine] LLM返回决策结果")
                
                # 验证决策格式
                if self._validate_decision(decision):
                    print("[DecisionEngine] 决策格式验证通过")
                    return decision
                else:
                    print("[DecisionEngine] 决策格式验证失败")
                    if attempt < self.max_retries - 1:
                        # 添加修正提示重试
                        messages.append({
                            "role": "user",
                            "content": "你的输出格式不正确，请严格按照JSON格式输出。"
                        })
                        print("[DecisionEngine] 正在重试...")
                    else:
                        # 最后一次尝试失败，返回默认决策
                        print("[DecisionEngine] 使用降级决策")
                        return self._fallback_decision(user_request)
                        
            except Exception as e:
                print(f"[DecisionEngine] LLM调用异常: {e}")
                if attempt < self.max_retries - 1:
                    continue
                else:
                    print("[DecisionEngine] 使用降级决策")
                    return self._fallback_decision(user_request)
        
        print("[DecisionEngine] 使用降级决策")
        return self._fallback_decision(user_request)
    
    def _format_session_context(self, session_context: Dict[str, Any]) -> str:
        """
        格式化会话上下文
        
        Args:
            session_context: 会话上下文
            
        Returns:
            str: 格式化的会话上下文文本
        """
        parts = []
        
        # 任务信息
        parts.append(f"### 任务状态")
        parts.append(f"- 任务ID: {session_context.get('thread_id', 'unknown')}")
        parts.append(f"- 任务类型: {session_context.get('task_type', 'unknown')}")
        parts.append(f"- 状态: {session_context.get('status', 'unknown')}")
        
        if session_context.get('current_goal'):
            parts.append(f"- 当前目标: {session_context['current_goal']}")
        
        # 工作摘要
        if session_context.get('working_summary'):
            parts.append(f"\n### 工作摘要")
            parts.append(session_context['working_summary'])
        
        # 最近对话
        if session_context.get('messages'):
            parts.append(f"\n### 最近对话")
            for msg in session_context['messages'][-6:]:  # 最近3轮
                role = "用户" if msg['role'] == 'user' else "助手"
                content = msg['content'][:150] + "..." if len(msg['content']) > 150 else msg['content']
                parts.append(f"[{role}] {content}")
        
        # 当前计划
        if session_context.get('plan'):
            parts.append(f"\n### 当前计划")
            for i, step in enumerate(session_context['plan'], 1):
                status = step.get('status', 'pending')
                parts.append(f"{i}. [{status}] {step.get('step', '')}")
        
        # 已读取文件
        if session_context.get('read_files'):
            parts.append(f"\n### 已读取文件")
            for f in session_context['read_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('reason', '')}")
        
        # 已修改文件
        if session_context.get('modified_files'):
            parts.append(f"\n### 已修改文件")
            for f in session_context['modified_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('change_summary', '')}")
        
        # 最近工具结果
        if session_context.get('tool_results'):
            parts.append(f"\n### 最近工具操作")
            for result in session_context['tool_results']:
                parts.append(f"- {result['tool']}: {result['summary'][:100]}")
        
        # 最近测试结果
        if session_context.get('test_results'):
            parts.append(f"\n### 最近测试结果")
            for test in session_context['test_results']:
                status = "通过" if test['passed'] else "失败"
                parts.append(f"- [{status}] {test['summary'][:100]}")
        
        # 待处理动作
        if session_context.get('pending_actions'):
            parts.append(f"\n### 待处理动作")
            for action in session_context['pending_actions']:
                parts.append(f"- {action}")
        
        # 错误信息
        if session_context.get('errors'):
            parts.append(f"\n### 最近错误")
            for error in session_context['errors'][-3:]:
                parts.append(f"- {error['error'][:100]}")
        
        return "\n".join(parts)
    
    def _validate_decision(self, decision: Dict[str, Any]) -> bool:
        """
        验证决策格式
        
        Args:
            decision: 决策结果
            
        Returns:
            bool: 是否有效
        """
        required_fields = [
            "request_type", "confidence", "summary",
            "needs_code_change", "needs_file_update",
            "target_files", "plan", "operations",
            "user_response_draft"
        ]
        
        for field in required_fields:
            if field not in decision:
                return False
        
        # 验证request_type
        valid_types = [
            "chat", "memory_update", "persona_update",
            "listen_policy_update", "capability_change",
            "code_change", "docs_update", "self_improvement"
        ]
        if decision["request_type"] not in valid_types:
            return False
        
        # 验证operations格式
        if not isinstance(decision["operations"], list):
            return False
        
        for op in decision["operations"]:
            if not isinstance(op, dict):
                return False
            if "type" not in op or "path" not in op:
                return False
        
        return True
    
    def _fallback_decision(self, user_request: str) -> Dict[str, Any]:
        """
        降级决策（当LLM调用失败时）
        
        Args:
            user_request: 用户请求
            
        Returns:
            dict: 降级决策
        """
        return {
            "request_type": "chat",
            "confidence": 0.5,
            "summary": f"LLM决策失败，降级为普通聊天: {user_request[:50]}...",
            "needs_code_change": False,
            "needs_file_update": False,
            "target_files": [],
            "read_more_files": [],
            "plan": ["降级处理：创建issue记录用户请求"],
            "operations": [
                {
                    "type": "create_issue",
                    "path": "self_development/issues.md",
                    "content": f"## [待处理] {user_request[:30]}...\n\n- 来源：用户请求\n- 原始输入：{user_request}\n- 状态：Open\n- 备注：LLM决策失败，需要人工处理",
                    "reason": "LLM决策失败，记录用户请求"
                }
            ],
            "tests_to_run": [],
            "user_response_draft": "抱歉，我暂时无法处理这个请求。已记录到待办事项中。"
        }
    
    def make_decision_with_more_files(
        self,
        user_request: str,
        first_decision: Dict[str, Any],
        more_files_content: Dict[str, str],
        session_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        读取更多文件后重新决策
        
        Args:
            user_request: 用户请求
            first_decision: 第一次决策
            more_files_content: 更多文件内容
            session_context: 会话上下文（短期记忆）
            
        Returns:
            dict: 最终决策
        """
        # 构建额外上下文
        additional_context = {
            "已读取的额外文件": "\n".join([
                f"### {path}\n{content[:2000]}..." if len(content) > 2000 else f"### {path}\n{content}"
                for path, content in more_files_content.items()
            ])
        }
        
        return self.make_decision(user_request, session_context, additional_context)


# 全局实例
_decision_engine = None


def get_decision_engine() -> DecisionEngine:
    """获取全局决策引擎实例"""
    global _decision_engine
    if _decision_engine is None:
        from core.llm_client import get_llm_client
        from core.context_builder import get_context_builder
        _decision_engine = DecisionEngine(get_llm_client(), get_context_builder())
    return _decision_engine
