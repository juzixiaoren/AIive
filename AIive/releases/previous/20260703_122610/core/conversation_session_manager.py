"""
会话记忆管理器模块

管理短期记忆（工作状态），实现 thread_state.json + event_log.jsonl 模式。
短期记忆不是聊天历史，而是当前任务的工作内存。
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


class ConversationSessionManager:
    """会话记忆管理器"""
    
    # 线程状态
    THREAD_STATUS = ["running", "waiting_user", "testing", "completed", "failed", "archived"]
    
    # 任务类型
    TASK_TYPES = ["chat", "memory_update", "self_development", "code_change", "capability_change", "persona_update", "listen_policy_update", "docs_update", "self_improvement"]
    
    def __init__(self, project_root: str, max_recent_messages: int = 10):
        """
        初始化会话记忆管理器
        
        Args:
            project_root: 项目根目录
            max_recent_messages: 保留的最近消息轮数（每轮包含用户和助手各一条）
        """
        self.project_root = Path(project_root)
        self.runtime_dir = self.project_root / "runtime"
        self.threads_dir = self.runtime_dir / "threads"
        self.events_dir = self.runtime_dir / "events"
        self.archived_dir = self.threads_dir / "archived"
        
        self.max_recent_messages = max_recent_messages * 2  # 转换为消息条数
        
        # 确保目录存在
        self._ensure_directories()
        
        # 当前活跃线程
        self._active_thread: Optional[Dict[str, Any]] = None
    
    def _ensure_directories(self):
        """确保必要目录存在"""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.archived_dir.mkdir(parents=True, exist_ok=True)
    
    def _generate_thread_id(self) -> str:
        """生成线程ID"""
        now = datetime.now()
        return f"thread_{now.strftime('%Y%m%d_%H%M%S')}"
    
    def _get_event_log_path(self) -> Path:
        """获取当前日期的事件日志路径"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.events_dir / f"{today}.jsonl"
    
    def _append_event(self, event: Dict[str, Any]):
        """
        追加事件到事件日志
        
        Args:
            event: 事件数据
        """
        event["time"] = datetime.now().isoformat()
        log_path = self._get_event_log_path()
        
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    
    def create_thread(self, user_request: str, task_type: str = "chat") -> Dict[str, Any]:
        """
        创建新线程
        
        Args:
            user_request: 用户请求
            task_type: 任务类型
            
        Returns:
            dict: 线程状态
        """
        # 如果有活跃线程且未归档，先归档
        if self._active_thread and self._active_thread.get("status") not in ["archived"]:
            self.archive_thread(self._active_thread["thread_id"])
        
        thread_id = self._generate_thread_id()
        now = datetime.now().isoformat()
        
        thread_state = {
            "thread_id": thread_id,
            "created_at": now,
            "updated_at": now,
            "status": "running",
            "task_type": task_type,
            
            "user_request": user_request,
            "current_goal": "",
            "working_summary": "",
            
            "messages": [
                {
                    "role": "user",
                    "content": user_request,
                    "time": now
                }
            ],
            
            "plan": [],
            
            "read_files": [],
            "modified_files": [],
            
            "tool_results": [],
            "test_results": [],
            
            "pending_actions": [],
            "errors": [],
            
            "last_llm_decision": None
        }
        
        # 保存线程状态
        self._save_thread(thread_state)
        
        # 记录事件
        self._append_event({
            "type": "thread_created",
            "thread_id": thread_id,
            "task_type": task_type,
            "user_request": user_request
        })
        
        self._active_thread = thread_state
        return thread_state
    
    def load_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        加载线程状态
        
        Args:
            thread_id: 线程ID
            
        Returns:
            dict: 线程状态，如果不存在返回 None
        """
        thread_path = self.threads_dir / f"{thread_id}.json"
        
        if not thread_path.exists():
            return None
        
        with open(thread_path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _save_thread(self, thread_state: Dict[str, Any]):
        """
        保存线程状态
        
        Args:
            thread_state: 线程状态
        """
        thread_state["updated_at"] = datetime.now().isoformat()
        thread_id = thread_state["thread_id"]
        thread_path = self.threads_dir / f"{thread_id}.json"
        
        with open(thread_path, "w", encoding="utf-8") as f:
            json.dump(thread_state, f, ensure_ascii=False, indent=2)
        
        # 更新活跃线程引用
        if self._active_thread and self._active_thread.get("thread_id") == thread_id:
            self._active_thread = thread_state
    
    def get_active_thread(self) -> Optional[Dict[str, Any]]:
        """
        获取当前活跃线程
        
        Returns:
            dict: 活跃线程状态，如果没有返回 None
        """
        # 如果已有活跃线程引用，直接返回
        if self._active_thread:
            return self._active_thread
        
        # 否则从文件加载最新的活跃线程
        active_thread_path = self.threads_dir / "active_thread.json"
        
        if active_thread_path.exists():
            with open(active_thread_path, "r", encoding="utf-8") as f:
                thread_id = json.load(f).get("thread_id")
                if thread_id:
                    thread = self.load_thread(thread_id)
                    if thread and thread.get("status") not in ["completed", "failed", "archived"]:
                        self._active_thread = thread
                        return thread
        
        return None
    
    def set_active_thread(self, thread_id: str):
        """
        设置活跃线程
        
        Args:
            thread_id: 线程ID
        """
        active_thread_path = self.threads_dir / "active_thread.json"
        with open(active_thread_path, "w", encoding="utf-8") as f:
            json.dump({"thread_id": thread_id}, f)
    
    def load_or_create_thread(self, user_request: str, task_type: str = "chat") -> Dict[str, Any]:
        """
        加载或创建线程
        
        如果当前有活跃线程且任务未完成，继续使用；否则创建新线程。
        
        Args:
            user_request: 用户请求
            task_type: 任务类型
            
        Returns:
            dict: 线程状态
        """
        active_thread = self.get_active_thread()
        
        # 如果没有活跃线程或任务已完成，创建新线程
        if not active_thread or active_thread.get("status") in ["completed", "failed", "archived"]:
            return self.create_thread(user_request, task_type)
        
        # 否则继续使用当前线程
        return active_thread
    
    def append_message(self, thread_id: str, role: str, content: str):
        """
        追加消息到线程
        
        Args:
            thread_id: 线程ID
            role: 角色（user/assistant/tool/system）
            content: 消息内容
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["messages"].append({
            "role": role,
            "content": content,
            "time": now
        })
        
        # 如果消息过多，更新 working_summary 并清理旧消息
        if len(thread["messages"]) > self.max_recent_messages:
            self._compress_messages(thread)
        
        self._save_thread(thread)
        
        # 记录事件
        self._append_event({
            "type": "message",
            "thread_id": thread_id,
            "role": role,
            "content": content[:200] + "..." if len(content) > 200 else content
        })
    
    def _compress_messages(self, thread: Dict[str, Any]):
        """
        压缩消息，保留最近的消息，将较早的消息总结到 working_summary
        
        Args:
            thread: 线程状态
        """
        messages = thread["messages"]
        
        # 需要压缩的消息（除最近的 max_recent_messages 条）
        messages_to_compress = messages[:-self.max_recent_messages]
        recent_messages = messages[-self.max_recent_messages:]
        
        # 生成压缩摘要
        if messages_to_compress:
            # 提取用户和助手的对话
            conversation_text = []
            for msg in messages_to_compress:
                role = "用户" if msg["role"] == "user" else "助手"
                conversation_text.append(f"{role}: {msg['content'][:100]}")
            
            # 更新 working_summary
            existing_summary = thread.get("working_summary", "")
            new_summary = "\n".join(conversation_text)
            
            if existing_summary:
                thread["working_summary"] = f"{existing_summary}\n\n---\n\n{new_summary}"
            else:
                thread["working_summary"] = new_summary
            
            # 只保留最近的消息
            thread["messages"] = recent_messages
    
    def update_goal(self, thread_id: str, goal: str):
        """
        更新当前目标
        
        Args:
            thread_id: 线程ID
            goal: 目标描述
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        thread["current_goal"] = goal
        self._save_thread(thread)
        
        self._append_event({
            "type": "goal_updated",
            "thread_id": thread_id,
            "goal": goal
        })
    
    def update_plan(self, thread_id: str, plan: List[Dict[str, str]]):
        """
        更新计划
        
        Args:
            thread_id: 线程ID
            plan: 计划步骤列表
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        thread["plan"] = plan
        self._save_thread(thread)
        
        self._append_event({
            "type": "plan_updated",
            "thread_id": thread_id,
            "plan_count": len(plan)
        })
    
    def add_read_file(self, thread_id: str, path: str, reason: str = ""):
        """
        记录读取的文件
        
        Args:
            thread_id: 线程ID
            path: 文件路径
            reason: 读取原因
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["read_files"].append({
            "path": path,
            "reason": reason,
            "time": now
        })
        
        self._save_thread(thread)
        
        self._append_event({
            "type": "file_read",
            "thread_id": thread_id,
            "path": path,
            "reason": reason
        })
    
    def add_modified_file(self, thread_id: str, path: str, change_summary: str = ""):
        """
        记录修改的文件
        
        Args:
            thread_id: 线程ID
            path: 文件路径
            change_summary: 修改摘要
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["modified_files"].append({
            "path": path,
            "change_summary": change_summary,
            "time": now
        })
        
        self._save_thread(thread)
        
        self._append_event({
            "type": "file_modified",
            "thread_id": thread_id,
            "path": path,
            "change_summary": change_summary
        })
    
    def add_tool_result(self, thread_id: str, tool: str, input_data: str, summary: str):
        """
        记录工具执行结果
        
        Args:
            thread_id: 线程ID
            tool: 工具名称
            input_data: 输入数据
            summary: 结果摘要
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["tool_results"].append({
            "tool": tool,
            "input": input_data[:200] if len(input_data) > 200 else input_data,
            "summary": summary,
            "time": now
        })
        
        # 保留最近的工具结果
        if len(thread["tool_results"]) > 20:
            thread["tool_results"] = thread["tool_results"][-20:]
        
        self._save_thread(thread)
        
        self._append_event({
            "type": "tool_result",
            "thread_id": thread_id,
            "tool": tool,
            "summary": summary
        })
    
    def add_test_result(self, thread_id: str, command: str, passed: bool, summary: str):
        """
        记录测试结果
        
        Args:
            thread_id: 线程ID
            command: 测试命令
            passed: 是否通过
            summary: 结果摘要
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["test_results"].append({
            "command": command,
            "passed": passed,
            "summary": summary,
            "time": now
        })
        
        self._save_thread(thread)
        
        self._append_event({
            "type": "test_result",
            "thread_id": thread_id,
            "command": command,
            "passed": passed,
            "summary": summary
        })
    
    def update_working_summary(self, thread_id: str, summary: str = None):
        """
        更新工作摘要
        
        Args:
            thread_id: 线程ID
            summary: 摘要内容，如果为 None 则自动生成
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        if summary is None:
            # 自动生成摘要
            summary = self._generate_working_summary(thread)
        
        thread["working_summary"] = summary
        self._save_thread(thread)
    
    def _generate_working_summary(self, thread: Dict[str, Any]) -> str:
        """
        自动生成工作摘要
        
        Args:
            thread: 线程状态
            
        Returns:
            str: 工作摘要
        """
        parts = []
        
        # 任务目标
        if thread.get("current_goal"):
            parts.append(f"目标: {thread['current_goal']}")
        
        # 读取的文件
        if thread.get("read_files"):
            files = [f["path"] for f in thread["read_files"][-5:]]
            parts.append(f"已读取文件: {', '.join(files)}")
        
        # 修改的文件
        if thread.get("modified_files"):
            files = [f["path"] for f in thread["modified_files"][-5:]]
            parts.append(f"已修改文件: {', '.join(files)}")
        
        # 最近的工具结果
        if thread.get("tool_results"):
            last_result = thread["tool_results"][-1]
            parts.append(f"最近工具操作: {last_result['tool']} - {last_result['summary'][:100]}")
        
        # 最近的测试结果
        if thread.get("test_results"):
            last_test = thread["test_results"][-1]
            status = "通过" if last_test["passed"] else "失败"
            parts.append(f"最近测试: {status} - {last_test['summary'][:100]}")
        
        # 待处理动作
        if thread.get("pending_actions"):
            actions = thread["pending_actions"][:3]
            parts.append(f"待处理: {', '.join(actions)}")
        
        return "\n".join(parts)
    
    def update_last_decision(self, thread_id: str, decision: Dict[str, Any]):
        """
        更新最后的 LLM 决策
        
        Args:
            thread_id: 线程ID
            decision: LLM 决策
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        thread["last_llm_decision"] = decision
        self._save_thread(thread)
    
    def update_status(self, thread_id: str, status: str):
        """
        更新线程状态
        
        Args:
            thread_id: 线程ID
            status: 新状态
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        thread["status"] = status
        self._save_thread(thread)
        
        self._append_event({
            "type": "status_changed",
            "thread_id": thread_id,
            "status": status
        })
    
    def add_error(self, thread_id: str, error: str):
        """
        记录错误
        
        Args:
            thread_id: 线程ID
            error: 错误信息
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        now = datetime.now().isoformat()
        thread["errors"].append({
            "error": error,
            "time": now
        })
        
        # 保留最近的错误
        if len(thread["errors"]) > 10:
            thread["errors"] = thread["errors"][-10:]
        
        self._save_thread(thread)
        
        self._append_event({
            "type": "error",
            "thread_id": thread_id,
            "error": error
        })
    
    def add_pending_action(self, thread_id: str, action: str):
        """
        添加待处理动作
        
        Args:
            thread_id: 线程ID
            action: 动作描述
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        if action not in thread["pending_actions"]:
            thread["pending_actions"].append(action)
            self._save_thread(thread)
    
    def remove_pending_action(self, thread_id: str, action: str):
        """
        移除待处理动作
        
        Args:
            thread_id: 线程ID
            action: 动作描述
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        if action in thread["pending_actions"]:
            thread["pending_actions"].remove(action)
            self._save_thread(thread)
    
    def archive_thread(self, thread_id: str):
        """
        归档线程
        
        Args:
            thread_id: 线程ID
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return
        
        # 更新状态为归档
        thread["status"] = "archived"
        
        # 移动到归档目录
        archived_path = self.archived_dir / f"{thread_id}.json"
        with open(archived_path, "w", encoding="utf-8") as f:
            json.dump(thread, f, ensure_ascii=False, indent=2)
        
        # 删除原文件
        original_path = self.threads_dir / f"{thread_id}.json"
        if original_path.exists():
            original_path.unlink()
        
        # 清除活跃线程引用
        if self._active_thread and self._active_thread.get("thread_id") == thread_id:
            self._active_thread = None
        
        # 清除活跃线程文件
        active_thread_path = self.threads_dir / "active_thread.json"
        if active_thread_path.exists():
            active_thread_path.unlink()
        
        self._append_event({
            "type": "thread_archived",
            "thread_id": thread_id
        })
    
    def get_context_for_llm(self, thread_id: str) -> Dict[str, Any]:
        """
        获取用于 LLM 的上下文
        
        Args:
            thread_id: 线程ID
            
        Returns:
            dict: 上下文信息
        """
        thread = self.load_thread(thread_id)
        if not thread:
            return {}
        
        return {
            "thread_id": thread["thread_id"],
            "task_type": thread["task_type"],
            "status": thread["status"],
            "user_request": thread["user_request"],
            "current_goal": thread["current_goal"],
            "working_summary": thread["working_summary"],
            "messages": thread["messages"],
            "plan": thread["plan"],
            "read_files": thread["read_files"],
            "modified_files": thread["modified_files"],
            "tool_results": thread["tool_results"][-5:],  # 只保留最近5个
            "test_results": thread["test_results"][-3:],  # 只保留最近3个
            "pending_actions": thread["pending_actions"],
            "errors": thread["errors"][-5:],  # 只保留最近5个
            "last_llm_decision": thread["last_llm_decision"]
        }
    
    def format_thread_context_for_llm(self, thread_id: str) -> str:
        """
        将线程上下文格式化为 LLM 可读的文本
        
        Args:
            thread_id: 线程ID
            
        Returns:
            str: 格式化的上下文文本
        """
        context = self.get_context_for_llm(thread_id)
        if not context:
            return ""
        
        parts = []
        
        # 任务信息
        parts.append("## 当前任务状态")
        parts.append(f"- 任务类型: {context['task_type']}")
        parts.append(f"- 状态: {context['status']}")
        parts.append(f"- 用户请求: {context['user_request']}")
        
        if context['current_goal']:
            parts.append(f"- 当前目标: {context['current_goal']}")
        
        parts.append("")
        
        # 工作摘要
        if context['working_summary']:
            parts.append("## 工作摘要")
            parts.append(context['working_summary'])
            parts.append("")
        
        # 最近对话
        if context['messages']:
            parts.append("## 最近对话")
            for msg in context['messages']:
                role = "用户" if msg['role'] == 'user' else "助手"
                parts.append(f"[{role}] {msg['content'][:200]}")
            parts.append("")
        
        # 当前计划
        if context['plan']:
            parts.append("## 当前计划")
            for i, step in enumerate(context['plan'], 1):
                status = step.get('status', 'pending')
                parts.append(f"{i}. [{status}] {step.get('step', '')}")
            parts.append("")
        
        # 已读取文件
        if context['read_files']:
            parts.append("## 已读取文件")
            for f in context['read_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('reason', '')}")
            parts.append("")
        
        # 已修改文件
        if context['modified_files']:
            parts.append("## 已修改文件")
            for f in context['modified_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('change_summary', '')}")
            parts.append("")
        
        # 最近工具结果
        if context['tool_results']:
            parts.append("## 最近工具操作")
            for result in context['tool_results']:
                parts.append(f"- {result['tool']}: {result['summary'][:100]}")
            parts.append("")
        
        # 最近测试结果
        if context['test_results']:
            parts.append("## 最近测试结果")
            for test in context['test_results']:
                status = "通过" if test['passed'] else "失败"
                parts.append(f"- [{status}] {test['summary'][:100]}")
            parts.append("")
        
        # 待处理动作
        if context['pending_actions']:
            parts.append("## 待处理动作")
            for action in context['pending_actions']:
                parts.append(f"- {action}")
            parts.append("")
        
        # 错误信息
        if context['errors']:
            parts.append("## 最近错误")
            for error in context['errors']:
                parts.append(f"- {error['error'][:100]}")
            parts.append("")
        
        return "\n".join(parts)


# 全局实例
_session_manager = None


def get_session_manager() -> ConversationSessionManager:
    """获取全局会话记忆管理器实例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = ConversationSessionManager(str(Path(__file__).parent.parent))
    return _session_manager
