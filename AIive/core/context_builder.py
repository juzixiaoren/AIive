"""
Context Builder模块

为LLM构建上下文，动态选择相关文件。
"""

from typing import List, Dict, Optional
from core.project_reader import ProjectReader


class ContextBuilder:
    """上下文构建器"""
    
    # 核心mind文件（必须包含）
    CORE_MIND_FILES = [
        "mind/persona.md",
        "mind/user_model.md",
        "mind/listen_policy.md",
        "mind/self_model.md"
    ]
    
    # 核心docs文件
    CORE_DOCS_FILES = [
        "docs/constitution.md",
        "docs/self_improvement_protocol.md"
    ]
    
    # 自我开发文件
    SELF_DEVELOPMENT_FILES = [
        "self_development/issues.md",
        "self_development/changelog.md"
    ]
    
    def __init__(self, project_reader: ProjectReader):
        """
        初始化上下文构建器
        
        Args:
            project_reader: 项目读取器
        """
        self.project_reader = project_reader
    
    def build_context(
        self,
        user_request: str,
        additional_files: Optional[List[str]] = None
    ) -> Dict[str, any]:
        """
        构建上下文
        
        Args:
            user_request: 用户请求
            additional_files: 额外需要包含的文件
            
        Returns:
            dict: 上下文信息
        """
        context = {
            "user_request": user_request,
            "mind_files": {},
            "docs_files": {},
            "self_development_files": {},
            "capabilities_summary": "",
            "recent_changelog": "",
            "project_tree": "",
            "additional_files": {}
        }
        
        # 读取mind文件
        for file_path in self.CORE_MIND_FILES:
            try:
                content = self.project_reader.read_file(file_path)
                context["mind_files"][file_path] = content
            except FileNotFoundError:
                context["mind_files"][file_path] = "[文件不存在]"
        
        # 读取docs文件（只读摘要，避免过长）
        for file_path in self.CORE_DOCS_FILES:
            try:
                content = self.project_reader.read_file(file_path)
                # 截取前2000字符作为摘要
                context["docs_files"][file_path] = content[:2000] + "\n..." if len(content) > 2000 else content
            except FileNotFoundError:
                context["docs_files"][file_path] = "[文件不存在]"
        
        # 读取自我开发文件
        for file_path in self.SELF_DEVELOPMENT_FILES:
            try:
                content = self.project_reader.read_file(file_path)
                # 截取前3000字符作为摘要
                context["self_development_files"][file_path] = content[:3000] + "\n..." if len(content) > 3000 else content
            except FileNotFoundError:
                context["self_development_files"][file_path] = "[文件不存在]"
        
        # 读取capabilities/registry.md摘要
        try:
            registry_content = self.project_reader.read_file("capabilities/registry.md")
            context["capabilities_summary"] = registry_content[:1000] + "\n..." if len(registry_content) > 1000 else registry_content
        except FileNotFoundError:
            context["capabilities_summary"] = "[文件不存在]"
        
        # 读取最近changelog
        try:
            changelog_content = self.project_reader.read_file("self_development/changelog.md")
            # 取最后1000字符
            context["recent_changelog"] = changelog_content[-1000:] if len(changelog_content) > 1000 else changelog_content
        except FileNotFoundError:
            context["recent_changelog"] = "[文件不存在]"
        
        # 生成项目树摘要
        context["project_tree"] = self.project_reader.summarize_project_tree(max_depth=2)
        
        # 读取额外文件
        if additional_files:
            context["additional_files"] = self.project_reader.read_files(additional_files)
        
        return context
    
    def format_context_for_llm(self, context: Dict[str, any]) -> str:
        """
        将上下文格式化为LLM可读的文本
        
        Args:
            context: 上下文信息
            
        Returns:
            str: 格式化的上下文文本
        """
        parts = []
        
        # 用户请求
        parts.append("## 用户请求\n")
        parts.append(context["user_request"])
        parts.append("\n\n")
        
        # 项目树
        parts.append("## 项目结构\n")
        parts.append("```\n")
        parts.append(context["project_tree"])
        parts.append("\n```\n\n")
        
        # Mind文件
        parts.append("## Mind文件\n")
        for file_path, content in context["mind_files"].items():
            parts.append(f"### {file_path}\n")
            parts.append(content)
            parts.append("\n\n")
        
        # 记忆文件（如果有）
        if context.get("memory_files"):
            parts.append("## 记忆文件\n")
            for file_path, content in context["memory_files"].items():
                parts.append(f"### {file_path}\n")
                parts.append(content)
                parts.append("\n\n")
        
        # Docs文件
        parts.append("## 核心文档\n")
        for file_path, content in context["docs_files"].items():
            parts.append(f"### {file_path}\n")
            parts.append(content)
            parts.append("\n\n")
        
        # 自我开发文件
        if context.get("self_development_files"):
            parts.append("## 自我开发任务\n")
            for file_path, content in context["self_development_files"].items():
                parts.append(f"### {file_path}\n")
                parts.append(content)
                parts.append("\n\n")
        
        # Capabilities
        parts.append("## 能力注册表\n")
        parts.append(context["capabilities_summary"])
        parts.append("\n\n")
        
        # Changelog
        parts.append("## 最近变更记录\n")
        parts.append(context["recent_changelog"])
        parts.append("\n\n")
        
        # 额外文件
        if context["additional_files"]:
            parts.append("## 相关文件\n")
            for file_path, content in context["additional_files"].items():
                parts.append(f"### {file_path}\n")
                parts.append(content)
                parts.append("\n\n")
        
        return "".join(parts)
    
    def build_decision_context(
        self,
        user_request: str,
        additional_files: Optional[List[str]] = None
    ) -> str:
        """
        构建用于决策的上下文
        
        Args:
            user_request: 用户请求
            additional_files: 额外文件
            
        Returns:
            str: 格式化的上下文
        """
        context = self.build_context(user_request, additional_files)
        return self.format_context_for_llm(context)
    
    def format_session_context_for_llm(self, session_context: Dict[str, any]) -> str:
        """
        将会话上下文格式化为LLM可读的文本
        
        Args:
            session_context: 会话上下文
            
        Returns:
            str: 格式化的会话上下文文本
        """
        if not session_context:
            return ""
        
        parts = []
        
        # 任务信息
        parts.append("## 当前任务状态\n")
        parts.append(f"- 任务ID: {session_context.get('thread_id', 'unknown')}")
        parts.append(f"- 任务类型: {session_context.get('task_type', 'unknown')}")
        parts.append(f"- 状态: {session_context.get('status', 'unknown')}")
        
        if session_context.get('current_goal'):
            parts.append(f"- 当前目标: {session_context['current_goal']}")
        
        parts.append("")
        
        # 工作摘要
        if session_context.get('working_summary'):
            parts.append("## 工作摘要\n")
            parts.append(session_context['working_summary'])
            parts.append("")
        
        # 最近对话
        if session_context.get('messages'):
            parts.append("## 最近对话\n")
            for msg in session_context['messages'][-6:]:  # 最近3轮
                role = "用户" if msg['role'] == 'user' else "助手"
                content = msg['content'][:200] + "..." if len(msg['content']) > 200 else msg['content']
                parts.append(f"[{role}] {content}")
            parts.append("")
        
        # 当前计划
        if session_context.get('plan'):
            parts.append("## 当前计划\n")
            for i, step in enumerate(session_context['plan'], 1):
                status = step.get('status', 'pending')
                parts.append(f"{i}. [{status}] {step.get('step', '')}")
            parts.append("")
        
        # 已读取文件
        if session_context.get('read_files'):
            parts.append("## 已读取文件\n")
            for f in session_context['read_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('reason', '')}")
            parts.append("")
        
        # 已修改文件
        if session_context.get('modified_files'):
            parts.append("## 已修改文件\n")
            for f in session_context['modified_files'][-5:]:
                parts.append(f"- {f['path']}: {f.get('change_summary', '')}")
            parts.append("")
        
        # 最近工具结果
        if session_context.get('tool_results'):
            parts.append("## 最近工具操作\n")
            for result in session_context['tool_results']:
                parts.append(f"- {result['tool']}: {result['summary'][:100]}")
            parts.append("")
        
        # 最近测试结果
        if session_context.get('test_results'):
            parts.append("## 最近测试结果\n")
            for test in session_context['test_results']:
                status = "通过" if test['passed'] else "失败"
                parts.append(f"- [{status}] {test['summary'][:100]}")
            parts.append("")
        
        # 待处理动作
        if session_context.get('pending_actions'):
            parts.append("## 待处理动作\n")
            for action in session_context['pending_actions']:
                parts.append(f"- {action}")
            parts.append("")
        
        # 错误信息
        if session_context.get('errors'):
            parts.append("## 最近错误\n")
            for error in session_context['errors'][-3:]:
                parts.append(f"- {error['error'][:100]}")
            parts.append("")
        
        return "".join(parts)
    
    def build_patch_context(
        self,
        user_request: str,
        target_files: List[str]
    ) -> str:
        """
        构建用于生成patch的上下文
        
        Args:
            user_request: 用户请求
            target_files: 需要修改的文件列表
            
        Returns:
            str: 格式化的上下文
        """
        context = self.build_context(user_request, target_files)
        return self.format_context_for_llm(context)


# 全局实例
_context_builder = None


def get_context_builder() -> ContextBuilder:
    """获取全局上下文构建器实例"""
    global _context_builder
    if _context_builder is None:
        from core.project_reader import get_project_reader
        _context_builder = ContextBuilder(get_project_reader())
    return _context_builder
