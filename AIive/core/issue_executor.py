"""
Issue Executor模块

解析 self_development/issues.md 中的任务，提取 Open 状态任务，提供给 LLM 分析和执行。
"""

import re
from pathlib import Path
from typing import Any


class IssueExecutor:
    """Issue 执行器"""

    def __init__(self, project_root: str | None = None) -> None:
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent

    def parse_issues(self, issues_content: str) -> list[dict[str, Any]]:
        """
        解析 issues.md 内容，提取任务列表

        Args:
            issues_content: issues.md 文件内容

        Returns:
            任务列表，每个任务包含 title, status, source, original_input, notes 等字段
        """
        tasks = []
        current_task = None

        for line in issues_content.split("\n"):
            line = line.strip()

            # 检测任务标题行：## [状态] 标题
            title_match = re.match(r"^## \[(.+?)\] (.+)$", line)
            if title_match:
                # 保存之前的任务
                if current_task:
                    tasks.append(current_task)

                status = title_match.group(1).strip()
                title = title_match.group(2).strip()
                current_task = {
                    "title": title,
                    "status": status,
                    "source": "",
                    "original_input": "",
                    "notes": ""
                }
                continue

            # 解析任务属性
            if current_task:
                if line.startswith("- 来源："):
                    current_task["source"] = line.replace("- 来源：", "").strip()
                elif line.startswith("- 原始输入："):
                    current_task["original_input"] = line.replace("- 原始输入：", "").strip()
                elif line.startswith("- 状态："):
                    current_task["status"] = line.replace("- 状态：", "").strip()
                elif line.startswith("- 备注："):
                    current_task["notes"] = line.replace("- 备注：", "").strip()

        # 保存最后一个任务
        if current_task:
            tasks.append(current_task)

        return tasks

    def get_open_tasks(self, issues_content: str) -> list[dict[str, Any]]:
        """
        获取 Open 状态的任务

        Args:
            issues_content: issues.md 文件内容

        Returns:
            Open 状态的任务列表
        """
        all_tasks = self.parse_issues(issues_content)
        return [t for t in all_tasks if t["status"].lower() in ["open", "待处理"]]

    def format_tasks_for_llm(self, tasks: list[dict[str, Any]]) -> str:
        """
        将任务列表格式化为 LLM 可读的文本

        Args:
            tasks: 任务列表

        Returns:
            格式化的文本
        """
        if not tasks:
            return "没有待处理的任务。"

        parts = ["## 待处理任务列表\n"]
        for i, task in enumerate(tasks, 1):
            parts.append(f"### 任务 {i}: {task['title']}")
            parts.append(f"- 状态: {task['status']}")
            parts.append(f"- 来源: {task['source']}")
            parts.append(f"- 原始输入: {task['original_input']}")
            if task['notes']:
                parts.append(f"- 备注: {task['notes']}")
            parts.append("")

        return "\n".join(parts)

    def update_task_status(
        self,
        issues_content: str,
        task_title: str,
        new_status: str,
        notes: str | None = None
    ) -> str:
        """
        更新任务状态

        Args:
            issues_content: 原始 issues.md 内容
            task_title: 任务标题
            new_status: 新状态
            notes: 新备注（可选）

        Returns:
            更新后的 issues.md 内容
        """
        lines = issues_content.split("\n")
        result_lines = []
        in_target_task = False
        task_found = False

        for line in lines:
            stripped = line.strip()

            # 检测任务标题行
            title_match = re.match(r"^## \[(.+?)\] (.+)$", stripped)
            if title_match:
                title = title_match.group(2).strip()
                if title == task_title:
                    in_target_task = True
                    task_found = True
                    # 更新状态
                    result_lines.append(f"## [{new_status}] {title}")
                    continue
                else:
                    in_target_task = False

            # 在目标任务中更新状态和备注
            if in_target_task and stripped.startswith("- 状态："):
                result_lines.append(f"- 状态：{new_status}")
                continue
            elif in_target_task and notes and stripped.startswith("- 备注："):
                result_lines.append(f"- 备注：{notes}")
                continue

            result_lines.append(line)

        if not task_found:
            # 任务未找到，添加新任务
            result_lines.append("")
            result_lines.append(f"## [{new_status}] {task_title}")
            result_lines.append("")
            result_lines.append(f"- 来源：自动更新")
            result_lines.append(f"- 原始输入：{task_title}")
            result_lines.append(f"- 状态：{new_status}")
            if notes:
                result_lines.append(f"- 备注：{notes}")

        return "\n".join(result_lines)


# 全局实例
_issue_executor: IssueExecutor | None = None


def get_issue_executor() -> IssueExecutor:
    """获取全局 Issue 执行器实例"""
    global _issue_executor
    if _issue_executor is None:
        _issue_executor = IssueExecutor()
    return _issue_executor
