"""
Agent Loop模块

负责CLI主循环，接入LLM决策流。
"""

import sys
from pathlib import Path
from typing import Any

from core.file_store import FileStore
from core.project_reader import ProjectReader
from core.context_builder import ContextBuilder
from core.decision_engine import DecisionEngine
from core.update_executor import UpdateExecutor
from core.llm_client import LLMClient
from core.self_repair_loop import SelfRepairLoop
from core.conversation_session_manager import ConversationSessionManager
from core.issue_executor import IssueExecutor
from kernel.version_manager import VersionManager
from kernel.test_runner import TestRunner
from kernel.health_check import HealthCheck


class AgentLoop:
    """Agent主循环"""
    
    def __init__(self, project_root=None):
        """
        初始化Agent主循环
        
        Args:
            project_root: 项目根目录
        """
        if project_root is None:
            self.project_root = Path(__file__).parent.parent
        else:
            self.project_root = Path(project_root)
        
        # 初始化各个模块
        self.file_store = FileStore(self.project_root)
        self.project_reader = ProjectReader(str(self.project_root))
        self.context_builder = ContextBuilder(self.project_reader)
        self.update_executor = UpdateExecutor(str(self.project_root))
        self.version_manager = VersionManager(str(self.project_root))
        self.test_runner = TestRunner(str(self.project_root))
        self.health_check = HealthCheck(self.project_root)
        self.session_manager = ConversationSessionManager(str(self.project_root))
        self.issue_executor = IssueExecutor(str(self.project_root))
        
        # LLM相关模块（可能初始化失败）
        self.llm_client = None
        self.decision_engine = None
        self.self_repair_loop = None
        
        try:
            self.llm_client = LLMClient()
            self.decision_engine = DecisionEngine(self.llm_client, self.context_builder)
            self.self_repair_loop = SelfRepairLoop(self.llm_client, self.decision_engine)
        except ValueError as e:
            print(f"警告: LLM未配置，将以降级模式运行: {e}")
        
        # 启动时检查目录和必要文件
        self._initialize()
    
    def _initialize(self):
        """初始化Agent"""
        # 确保必要目录存在
        necessary_dirs = [
            "docs", "core", "mind",
            "memory/preferences", "memory/feedback", "memory/events",
            "memory/summaries", "memory/archive", "memory/index",
            "capabilities/local_tools", "capabilities/workflows",
            "capabilities/mcp", "capabilities/ui_components",
            "capabilities/dormant", "capabilities/deprecated",
            "self_development/experiments", "self_development/patches",
            "kernel", "tests"
        ]
        
        for dir_path in necessary_dirs:
            self.file_store.ensure_directory(dir_path)
        
        # 确保运行时目录存在
        self.version_manager._ensure_directories()
    
    def process_input(self, user_input: str, stream: bool = False) -> str:
        """
        处理用户输入
        
        Args:
            user_input: 用户输入文本
            stream: 是否使用流式输出
            
        Returns:
            str: Agent回复
        """
        if not user_input or not user_input.strip():
            return "请输入内容"
        
        user_input = user_input.strip()
        
        # 如果LLM未配置，降级处理
        if not self.llm_client or not self.decision_engine:
            return self._fallback_process(user_input)
        
        try:
            # 使用LLM决策
            return self._llm_process(user_input, stream=stream)
        except Exception as e:
            print(f"LLM处理失败，降级处理: {e}")
            return self._fallback_process(user_input)
    
    def _llm_process(self, user_input: str, stream: bool = False) -> str:
        """
        使用LLM处理用户输入
        
        Args:
            user_input: 用户输入
            stream: 是否使用流式输出
            
        Returns:
            str: 回复
        """
        print(f"[Agent] 正在分析用户请求: {user_input[:50]}...")
        
        # 加载或创建会话线程
        thread = self.session_manager.load_or_create_thread(user_input)
        thread_id = thread["thread_id"]
        print(f"[Session] 使用会话线程: {thread_id}")
        
        # 获取会话上下文
        session_context = self.session_manager.get_context_for_llm(thread_id)
        
        # 第一次决策
        print("[Agent] 正在调用LLM进行决策...")
        decision = self.decision_engine.make_decision(user_input, session_context)
        print(f"[Agent] 决策完成，请求类型: {decision.get('request_type', 'unknown')}")
        
        # 更新线程的最后决策
        self.session_manager.update_last_decision(thread_id, decision)
        
        # 如果需要读取更多文件
        if decision.get("read_more_files"):
            print(f"[Agent] 正在读取更多文件: {decision['read_more_files']}")
            more_files_content = self.project_reader.read_files(decision["read_more_files"])
            
            # 记录读取的文件
            for file_path in decision["read_more_files"]:
                self.session_manager.add_read_file(thread_id, file_path, "决策需要")
            
            print("[Agent] 已读取更多文件，正在重新决策...")
            decision = self.decision_engine.make_decision_with_more_files(
                user_input, decision, more_files_content, session_context
            )
            print(f"[Agent] 重新决策完成，请求类型: {decision.get('request_type', 'unknown')}")
            
            # 更新最后决策
            self.session_manager.update_last_decision(thread_id, decision)
        
        request_type = decision.get("request_type", "chat")
        operations = decision.get("operations", [])
        user_response_draft = decision.get("user_response_draft", "")
        
        # 更新任务类型和目标
        self.session_manager.update_goal(thread_id, decision.get("summary", user_input[:100]))
        
        # 如果只是聊天，直接返回
        if request_type == "chat" and not operations:
            if stream:
                return self._stream_chat_response(user_input)
            # 追加助手回复到会话
            response = user_response_draft or "我理解你的问题。"
            self.session_manager.append_message(thread_id, "assistant", response)
            return response
        
        # 特殊处理 complete_task 类型
        if request_type == "complete_task":
            print("[Agent] 正在处理任务完成请求...")
            return self._handle_complete_task(user_input, decision, thread_id, stream)
        
        # 如果不是chat但operations为空，需要补充操作
        if not operations and request_type != "chat":
            print(f"[Agent] 决策类型为 {request_type} 但没有操作，需要补充操作...")
            # 根据request_type生成默认操作
            operations = self._generate_default_operations(request_type, user_input, decision)
            
            # 如果仍然没有操作，返回用户回复
            if not operations:
                response = user_response_draft or f"我理解你想进行 {request_type} 类型的操作，但暂时没有具体的执行计划。"
                self.session_manager.append_message(thread_id, "assistant", response)
                if not stream:
                    print(f"\nAgent: {response}")
                return response
        
        # 如果需要修改文件
        if operations:
            print(f"[Agent] 正在执行操作，共 {len(operations)} 个操作...")
            
            # 记录读取的文件（如果有）
            for op in operations:
                if op.get("type") in ["read_file", "append_file", "write_file", "patch_file"]:
                    file_path = op.get("path", "")
                    if file_path:
                        self.session_manager.add_read_file(thread_id, file_path, f"操作类型: {op['type']}")
            
            # 检查是否需要代码修改
            needs_code_change = decision.get("needs_code_change", False)
            
            if needs_code_change:
                # 使用candidate工作区
                print("[Agent] 需要代码修改，使用candidate工作区...")
                result = self._execute_with_candidate(user_input, operations, decision)
            else:
                # 直接执行（不需要代码修改）
                print("[Agent] 直接执行文件操作...")
                result = self.update_executor.execute_operations(operations)
            
            # 记录工具结果
            results_list = result.get("results", [])
            for i, op in enumerate(operations):
                op_type = op.get("type", "unknown")
                op_path = op.get("path", "")
                if i < len(results_list):
                    success = results_list[i].get("success", False)
                else:
                    success = False
                
                summary = f"{'成功' if success else '失败'}: {op_type} {op_path}"
                self.session_manager.add_tool_result(thread_id, op_type, op_path, summary)
            
            # 记录修改的文件
            for op in operations:
                if op.get("type") in ["write_file", "append_file", "patch_file", "create_file"]:
                    file_path = op.get("path", "")
                    if file_path:
                        self.session_manager.add_modified_file(thread_id, file_path, op.get("reason", ""))
            
            if result["success"]:
                print("[Agent] 操作执行成功")
                # 更新self_model和changelog
                self._update_self_model_and_changelog(user_input, decision)
                
                # 更新线程状态为完成
                self.session_manager.update_status(thread_id, "completed")
                
                # 追加助手回复到会话
                response = user_response_draft or "操作已完成。"
                self.session_manager.append_message(thread_id, "assistant", response)
                
                if not stream:
                    print(f"\nAgent: {response}")
                return response
            else:
                print("[Agent] 操作执行失败")
                # 获取失败详情
                fail_details = []
                for r in result.get("results", []):
                    if not r.get("success", True):
                        error = r.get("error", "未知错误")
                        fail_details.append(error)
                
                error_msg = "; ".join(fail_details) if fail_details else "未知错误"
                
                # 记录错误
                self.session_manager.add_error(thread_id, error_msg)
                
                # 更新线程状态为失败
                self.session_manager.update_status(thread_id, "failed")
                
                # 追加助手回复到会话
                response = f"操作失败: {error_msg}"
                self.session_manager.append_message(thread_id, "assistant", response)
                
                if not stream:
                    print(f"\nAgent: {response}")
                return response
        
        # 追加助手回复到会话
        response = user_response_draft or "我不确定如何处理这个请求。"
        self.session_manager.append_message(thread_id, "assistant", response)
        
        if not stream:
            print(f"\nAgent: {response}")
        return response
    
    def _stream_chat_response(self, user_input: str) -> str:
        """
        流式输出聊天回复
        
        Args:
            user_input: 用户输入
            
        Returns:
            str: 完整回复
        """
        # 获取当前线程的会话上下文
        thread = self.session_manager.get_active_thread()
        session_context = {}
        if thread:
            session_context = self.session_manager.get_context_for_llm(thread["thread_id"])
        
        # 构建上下文
        context_text = self.context_builder.build_decision_context(user_input)
        
        # 添加会话上下文
        if session_context:
            context_text += "\n\n## 会话历史\n"
            context_text += self.session_manager.format_thread_context_for_llm(thread["thread_id"])
        
        # 构建消息
        messages = [
            {"role": "system", "content": "你是一个有帮助的AI助手。请简洁、直接地回复用户。"},
            {"role": "user", "content": context_text}
        ]
        
        # 使用流式输出
        full_response = ""
        print("\nAgent: ", end="", flush=True)
        
        for chunk in self.llm_client.chat_stream(messages, temperature=0.7):
            print(chunk, end="", flush=True)
            full_response += chunk
        
        print()  # 换行
        
        # 追加助手回复到会话
        if thread:
            self.session_manager.append_message(thread["thread_id"], "assistant", full_response)
        
        return full_response
    
    def _execute_with_candidate(
        self,
        user_input: str,
        operations: list,
        decision: dict[str, Any]
    ) -> dict[str, Any]:
        """
        使用candidate工作区执行操作
        
        Args:
            user_input: 用户输入
            operations: 操作列表
            decision: 决策
            
        Returns:
            dict: 执行结果
        """
        # 使用self_repair_loop执行
        if self.self_repair_loop:
            return self.self_repair_loop.run_repair_cycle(
                user_input,
                operations,
                self.version_manager,
                self.test_runner,
                decision
            )
        else:
            # 如果没有self_repair_loop，直接执行
            return self.update_executor.execute_operations(operations)
    
    def _update_self_model_and_changelog(
        self,
        user_input: str,
        decision: dict[str, Any]
    ):
        """
        更新self_model和changelog
        
        Args:
            user_input: 用户输入
            decision: 决策
        """
        request_type = decision.get("request_type", "unknown")
        summary = decision.get("summary", user_input[:50])
        
        # 使用execute_operations来更新self_model和changelog
        operations = [
            {
                "type": "update_self_model",
                "content": f"处理了{request_type}请求: {summary}"
            },
            {
                "type": "update_changelog",
                "content": f"类型: {request_type}\n描述: {summary}\n操作数: {len(decision.get('operations', []))}"
            }
        ]
        
        self.update_executor.execute_operations(operations)
    
    def _generate_default_operations(
        self,
        request_type: str,
        user_input: str,
        decision: dict[str, Any]
    ) -> list:
        """
        根据request_type生成默认操作
        
        Args:
            request_type: 请求类型
            user_input: 用户输入
            decision: 决策结果
            
        Returns:
            list: 操作列表
        """
        operations = []
        
        if request_type == "self_improvement":
            # 自我改进请求，创建issue记录
            issue_content = f"""## [待处理] {user_input[:50]}...

- 来源：用户请求
- 原始输入：{user_input}
- 状态：Open
- 备注：待分析和实施
"""
            operations.append({
                "type": "create_issue",
                "path": "self_development/issues.md",
                "content": issue_content,
                "reason": "记录自我改进请求"
            })
            
        elif request_type == "memory_update":
            # 记忆更新，创建issue记录具体需求
            issue_content = f"""## [待处理] 记忆更新: {user_input[:30]}...

- 来源：用户请求
- 原始输入：{user_input}
- 状态：Open
- 备注：需要确定具体的记忆文件和内容格式
"""
            operations.append({
                "type": "create_issue",
                "path": "self_development/issues.md",
                "content": issue_content,
                "reason": "记录记忆更新请求"
            })
            
        elif request_type == "capability_change":
            # 能力变更，创建issue记录
            issue_content = f"""## [待处理] 能力变更: {user_input[:30]}...

- 来源：用户请求
- 原始输入：{user_input}
- 状态：Open
- 备注：需要设计和实现新的能力
"""
            operations.append({
                "type": "create_issue",
                "path": "self_development/issues.md",
                "content": issue_content,
                "reason": "记录能力变更请求"
            })
        
        elif request_type == "complete_task":
            # 完成任务请求，读取 issues.md 并分析
            try:
                issues_content = self.project_reader.read_file("self_development/issues.md")
                open_tasks = self.issue_executor.get_open_tasks(issues_content)
                
                if not open_tasks:
                    print("[Agent] 没有待处理的任务")
                    return []
                
                # 格式化任务列表供 LLM 分析
                tasks_text = self.issue_executor.format_tasks_for_llm(open_tasks)
                print(f"[Agent] 发现 {len(open_tasks)} 个待处理任务")
                
                # 将任务信息作为额外上下文提供给 LLM
                # 这里我们返回一个 read_file 操作来读取 issues.md
                operations.append({
                    "type": "read_file",
                    "path": "self_development/issues.md",
                    "reason": "读取待处理任务"
                })
                
            except Exception as e:
                print(f"[Agent] 读取 issues.md 失败: {e}")
        
        return operations
    
    def _handle_complete_task(
        self,
        user_input: str,
        decision: dict[str, Any],
        thread_id: str,
        stream: bool = False
    ) -> str:
        """
        处理完成任务请求
        
        Args:
            user_input: 用户输入
            decision: 决策结果
            thread_id: 会话线程ID
            stream: 是否使用流式输出
            
        Returns:
            str: 回复
        """
        print("[Agent] 开始处理任务完成流程...")
        
        # 1. 读取 issues.md
        try:
            issues_content = self.project_reader.read_file("self_development/issues.md")
            open_tasks = self.issue_executor.get_open_tasks(issues_content)
            
            if not open_tasks:
                response = "没有待处理的任务。"
                self.session_manager.append_message(thread_id, "assistant", response)
                return response
            
            print(f"[Agent] 发现 {len(open_tasks)} 个待处理任务")
            
            # 2. 挑选一个任务（这里简单选择第一个，后续可以让 LLM 选择）
            selected_task = open_tasks[0]
            task_title = selected_task["title"]
            task_input = selected_task["original_input"]
            
            print(f"[Agent] 选择任务: {task_title}")
            print(f"[Agent] 任务描述: {task_input}")
            
            # 3. 发送进度消息
            progress_msg = f"正在处理任务: {task_title}\n\n任务描述: {task_input}"
            if not stream:
                print(f"\nAgent: {progress_msg}")
            
            # 4. 让 LLM 分析任务并生成代码修改操作
            # 构建额外上下文
            additional_context = {
                "待处理任务": self.issue_executor.format_tasks_for_llm([selected_task]),
                "任务要求": "请分析这个任务，生成具体的代码修改操作来实现它。"
            }
            
            # 重新决策
            print("[Agent] 正在让 LLM 分析任务并生成修改方案...")
            task_decision = self.decision_engine.make_decision(
                f"实现任务: {task_input}",
                session_context=self.session_manager.get_context_for_llm(thread_id),
                additional_context=additional_context
            )
            
            print(f"[Agent] LLM 决策完成，请求类型: {task_decision.get('request_type', 'unknown')}")
            
            # 5. 获取操作列表
            operations = task_decision.get("operations", [])
            
            if not operations:
                print("[Agent] LLM 没有生成任何操作")
                response = f"无法为任务 '{task_title}' 生成实现方案。"
                self.session_manager.append_message(thread_id, "assistant", response)
                return response
            
            print(f"[Agent] LLM 生成了 {len(operations)} 个操作")
            
            # 6. 执行操作
            needs_code_change = task_decision.get("needs_code_change", False)
            
            if needs_code_change:
                print("[Agent] 需要代码修改，使用candidate工作区...")
                result = self._execute_with_candidate(user_input, operations, task_decision)
            else:
                print("[Agent] 直接执行文件操作...")
                result = self.update_executor.execute_operations(operations)
            
            # 7. 检查结果
            if result["success"]:
                print("[Agent] 任务执行成功")
                
                # 8. 更新任务状态
                print("[Agent] 正在更新任务状态...")
                updated_content = self.issue_executor.update_task_status(
                    issues_content,
                    task_title,
                    "Completed",
                    f"已完成于 {self._get_timestamp()}"
                )
                
                # 写入更新后的 issues.md
                self.update_executor.execute_operations([{
                    "type": "write_file",
                    "path": "self_development/issues.md",
                    "content": updated_content,
                    "reason": "更新任务状态为已完成"
                }])
                
                # 9. 更新 self_model 和 changelog
                self._update_self_model_and_changelog(user_input, task_decision)
                
                # 10. 生成报告
                report = self._generate_task_report(task_title, task_input, operations, result)
                
                # 更新线程状态
                self.session_manager.update_status(thread_id, "completed")
                self.session_manager.append_message(thread_id, "assistant", report)
                
                if not stream:
                    print(f"\nAgent: {report}")
                return report
            else:
                print("[Agent] 任务执行失败")
                error_msg = self._extract_error_message(result)
                
                # 更新任务状态为失败
                updated_content = self.issue_executor.update_task_status(
                    issues_content,
                    task_title,
                    "Open",
                    f"执行失败: {error_msg}"
                )
                
                self.update_executor.execute_operations([{
                    "type": "write_file",
                    "path": "self_development/issues.md",
                    "content": updated_content,
                    "reason": "更新任务状态"
                }])
                
                response = f"任务 '{task_title}' 执行失败: {error_msg}"
                self.session_manager.update_status(thread_id, "failed")
                self.session_manager.append_message(thread_id, "assistant", response)
                
                if not stream:
                    print(f"\nAgent: {response}")
                return response
                
        except Exception as e:
            print(f"[Agent] 处理任务时发生错误: {e}")
            response = f"处理任务时发生错误: {str(e)}"
            self.session_manager.append_message(thread_id, "assistant", response)
            return response
    
    def _get_timestamp(self) -> str:
        """获取当前时间戳"""
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def _generate_task_report(
        self,
        task_title: str,
        task_input: str,
        operations: list,
        result: dict
    ) -> str:
        """
        生成任务执行报告
        
        Args:
            task_title: 任务标题
            task_input: 任务描述
            operations: 执行的操作列表
            result: 执行结果
            
        Returns:
            str: 报告内容
        """
        report_parts = [
            f"## 任务完成报告",
            f"",
            f"### 任务信息",
            f"- 任务: {task_title}",
            f"- 描述: {task_input}",
            f"",
            f"### 执行操作",
        ]
        
        for i, op in enumerate(operations, 1):
            op_type = op.get("type", "unknown")
            op_path = op.get("path", "")
            op_reason = op.get("reason", "")
            report_parts.append(f"{i}. {op_type} {op_path}")
            if op_reason:
                report_parts.append(f"   原因: {op_reason}")
        
        report_parts.extend([
            f"",
            f"### 执行结果",
            f"- 成功操作: {result.get('success_count', 0)}",
            f"- 失败操作: {result.get('fail_count', 0)}",
            f"",
            f"### 状态",
            f"任务已完成并更新状态。"
        ])
        
        return "\n".join(report_parts)
    
    def _extract_error_message(self, result: dict) -> str:
        """
        提取错误信息
        
        Args:
            result: 执行结果
            
        Returns:
            str: 错误信息
        """
        fail_details = []
        for r in result.get("results", []):
            if not r.get("success", True):
                error = r.get("error", "未知错误")
                fail_details.append(error)
        
        return "; ".join(fail_details) if fail_details else "未知错误"
    
    def _fallback_process(self, user_input: str) -> str:
        """
        降级处理（无LLM时）
        
        Args:
            user_input: 用户输入
            
        Returns:
            str: 回复
        """
        # 创建issue记录用户请求
        issue_content = f"""## [待处理] {user_input[:30]}...

- 来源：用户请求
- 原始输入：{user_input}
- 状态：Open
- 备注：LLM未配置，需要手动处理
"""
        
        self.update_executor.execute_operations([
            {
                "type": "create_issue",
                "path": "self_development/issues.md",
                "content": issue_content,
                "reason": "记录用户请求（LLM未配置）"
            }
        ])
        
        return f"抱歉，LLM未配置，无法智能处理您的请求。已记录到待办事项中。请配置 .env 文件中的 LLM_API_KEY。"
    
    def run_health_check(self) -> dict[str, Any]:
        """
        运行健康检查
        
        Returns:
            dict: 检查结果
        """
        return self.health_check.run_check()
    
    def get_status(self) -> dict[str, Any]:
        """
        获取状态
        
        Returns:
            dict: 状态信息
        """
        return {
            "project_root": str(self.project_root),
            "llm_configured": self.llm_client is not None,
            "version_manager": self.version_manager.get_status(),
            "health_check": self.run_health_check()
        }
    
    def run_tests(self) -> dict[str, Any]:
        """
        运行测试
        
        Returns:
            dict: 测试结果
        """
        return self.test_runner.run_pytest()


def main():
    """CLI入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="AIive - 自进化个人Agent自举内核")
    parser.add_argument("--health-check", action="store_true", help="运行健康检查")
    parser.add_argument("--self-status", action="store_true", help="显示状态")
    parser.add_argument("--run-tests", action="store_true", help="运行测试")
    
    args = parser.parse_args()
    
    agent = AgentLoop()
    
    if args.health_check:
        result = agent.run_health_check()
        print(f"健康检查: {'通过' if result['pass'] else '失败'}")
        if result['reasons']:
            for reason in result['reasons']:
                print(f"  - {reason}")
        return
    
    if args.self_status:
        status = agent.get_status()
        print("=== AIive 状态 ===")
        print(f"项目根目录: {status['project_root']}")
        print(f"LLM配置: {'已配置' if status['llm_configured'] else '未配置'}")
        print(f"健康检查: {'通过' if status['health_check']['pass'] else '失败'}")
        return
    
    if args.run_tests:
        result = agent.run_tests()
        print(f"测试: {'通过' if result['success'] else '失败'}")
        print(result.get('stdout', ''))
        return
    
    # 交互模式
    print("AIive - 自进化个人Agent自举内核")
    print("输入 'exit' 或 'quit' 退出")
    print("-" * 40)
    
    while True:
        try:
            user_input = input("\nUser: ")
            
            if user_input.lower() in ["exit", "quit"]:
                print("再见！")
                break
            
            print("[CLI] 正在处理用户输入...")
            # 使用流式输出
            response = agent.process_input(user_input, stream=True)
            print("[CLI] 处理完成")
            
        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"\n错误: {e}")


if __name__ == "__main__":
    main()
