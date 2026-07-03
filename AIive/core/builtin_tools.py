"""
内置工具模块

注册所有内置工具到工具注册表。
"""

from pathlib import Path
from typing import Any

from core.tool_registry import get_tool_registry


def register_builtin_tools(project_root: str | None = None) -> None:
    """注册所有内置工具"""
    registry = get_tool_registry()
    root = Path(project_root) if project_root else Path(__file__).parent.parent

    # 1. 读取文件
    def read_file(path: str) -> dict[str, Any]:
        """读取文件内容"""
        full_path = root / path
        if not full_path.exists():
            return {"success": False, "error": f"文件不存在: {path}"}
        try:
            content = full_path.read_text(encoding="utf-8")
            return {"success": True, "content": content, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="read_file",
        description="读取文件内容",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于项目根目录）"
                }
            },
            "required": ["path"]
        },
        func=read_file
    )

    # 2. 写入文件
    def write_file(path: str, content: str) -> dict[str, Any]:
        """写入文件内容"""
        full_path = root / path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="write_file",
        description="写入文件内容（覆盖）",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于项目根目录）"
                },
                "content": {
                    "type": "string",
                    "description": "文件内容"
                }
            },
            "required": ["path", "content"]
        },
        func=write_file
    )

    # 3. 追加文件
    def append_file(path: str, content: str) -> dict[str, Any]:
        """追加文件内容"""
        full_path = root / path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="append_file",
        description="追加文件内容",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于项目根目录）"
                },
                "content": {
                    "type": "string",
                    "description": "要追加的内容"
                }
            },
            "required": ["path", "content"]
        },
        func=append_file
    )

    # 4. 列出目录
    def list_directory(path: str = ".") -> dict[str, Any]:
        """列出目录内容"""
        full_path = root / path
        if not full_path.exists():
            return {"success": False, "error": f"目录不存在: {path}"}
        if not full_path.is_dir():
            return {"success": False, "error": f"不是目录: {path}"}
        try:
            items = []
            for item in full_path.iterdir():
                items.append({
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file"
                })
            return {"success": True, "path": path, "items": items}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="list_directory",
        description="列出目录内容",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（相对于项目根目录，默认为 '.'）",
                    "default": "."
                }
            }
        },
        func=list_directory
    )

    # 5. 运行测试
    def run_tests(test_path: str = "tests/") -> dict[str, Any]:
        """运行测试"""
        import subprocess
        try:
            result = subprocess.run(
                ["python3", "-m", "pytest", test_path, "-v"],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=120
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "测试执行超时"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="run_tests",
        description="运行测试",
        parameters={
            "type": "object",
            "properties": {
                "test_path": {
                    "type": "string",
                    "description": "测试路径（默认为 'tests/'）",
                    "default": "tests/"
                }
            }
        },
        func=run_tests
    )

    # 6. 更新任务状态
    def update_task_status(task_title: str, new_status: str, notes: str = "") -> dict[str, Any]:
        """更新 issues.md 中的任务状态"""
        from core.issue_executor import get_issue_executor

        issues_path = root / "self_development" / "issues.md"
        if not issues_path.exists():
            return {"success": False, "error": "issues.md 不存在"}

        try:
            content = issues_path.read_text(encoding="utf-8")
            executor = get_issue_executor()
            updated_content = executor.update_task_status(content, task_title, new_status, notes)
            issues_path.write_text(updated_content, encoding="utf-8")
            return {"success": True, "task": task_title, "status": new_status}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="update_task_status",
        description="更新 issues.md 中的任务状态",
        parameters={
            "type": "object",
            "properties": {
                "task_title": {
                    "type": "string",
                    "description": "任务标题"
                },
                "new_status": {
                    "type": "string",
                    "description": "新状态（Open/In Progress/Completed/Closed）"
                },
                "notes": {
                    "type": "string",
                    "description": "备注",
                    "default": ""
                }
            },
            "required": ["task_title", "new_status"]
        },
        func=update_task_status
    )

    # 7. 获取待处理任务
    def get_open_tasks() -> dict[str, Any]:
        """获取 issues.md 中的待处理任务"""
        from core.issue_executor import get_issue_executor

        issues_path = root / "self_development" / "issues.md"
        if not issues_path.exists():
            return {"success": False, "error": "issues.md 不存在"}

        try:
            content = issues_path.read_text(encoding="utf-8")
            executor = get_issue_executor()
            tasks = executor.get_open_tasks(content)
            return {"success": True, "tasks": tasks, "count": len(tasks)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    registry.register(
        name="get_open_tasks",
        description="获取待处理任务列表",
        parameters={
            "type": "object",
            "properties": {}
        },
        func=get_open_tasks
    )

    # 8. 发送消息给用户
    def send_message(message: str) -> dict[str, Any]:
        """发送消息给用户"""
        print(f"\n[Agent] {message}")
        return {"success": True, "message": message}

    registry.register(
        name="send_message",
        description="发送消息给用户",
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "消息内容"
                }
            },
            "required": ["message"]
        },
        func=send_message
    )

    print(f"[ToolRegistry] 已注册 {len(registry.get_all_tools())} 个内置工具")
