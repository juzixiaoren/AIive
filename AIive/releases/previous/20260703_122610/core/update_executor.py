"""
Update Executor模块

负责真实执行LLM决策的文件操作，禁止删除操作。
"""

from pathlib import Path
from typing import Any
from datetime import datetime

from core.utils import timeout


class UpdateExecutor:
    """更新执行器"""

    # 支持的操作类型
    SUPPORTED_OPERATIONS: set[str] = {
        "read_file",
        "write_file",
        "append_file",
        "create_file",
        "patch_file",
        "create_directory",
        "create_issue",
        "update_registry",
        "update_self_model",
        "update_changelog",
        "run_tests",
    }

    def __init__(self, project_root: str | None = None) -> None:
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent

    def validate_operations(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        验证操作列表，返回过滤后的合法操作。

        过滤掉不支持的操作类型和删除操作。

        Args:
            operations: 原始操作列表

        Returns:
            过滤后的合法操作列表
        """
        valid = []
        for op in operations:
            op_type = op.get("type", "")
            if op_type in ["delete_file", "remove_file", "delete_directory"]:
                continue
            if op_type not in self.SUPPORTED_OPERATIONS:
                continue
            valid.append(op)
        return valid

    @timeout(60)
    def execute_operations(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """
        执行操作列表。

        Args:
            operations: 操作列表

        Returns:
            执行结果
        """
        print(f"[UpdateExecutor] 正在执行 {len(operations)} 个操作...")
        results: list[dict[str, Any]] = []
        success_count = 0
        fail_count = 0

        for i, op in enumerate(operations):
            op_type = op.get("type", "unknown")
            path = op.get("path", "")
            print(f"[UpdateExecutor] 执行操作 {i+1}/{len(operations)}: {op_type} {path}")
            try:
                result = self._execute_single_operation(op)
                results.append(result)
                if result["success"]:
                    success_count += 1
                    print(f"[UpdateExecutor] 操作 {i+1} 成功")
                else:
                    fail_count += 1
                    print(f"[UpdateExecutor] 操作 {i+1} 失败: {result.get('error', '未知错误')}")
            except Exception as e:
                results.append({"success": False, "operation": op, "error": str(e)})
                fail_count += 1
                print(f"[UpdateExecutor] 操作 {i+1} 异常: {e}")

        print(f"[UpdateExecutor] 操作执行完成: {success_count} 成功, {fail_count} 失败")
        return {
            "success": fail_count == 0,
            "total": len(operations),
            "success_count": success_count,
            "fail_count": fail_count,
            "results": results,
        }

    def _execute_single_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        """
        执行单个操作。

        Args:
            operation: 操作

        Returns:
            执行结果
        """
        op_type = operation.get("type")
        path = operation.get("path", "")
        content = operation.get("content", "")

        # 安全检查：禁止删除操作
        if op_type in ["delete_file", "remove_file", "delete_directory"]:
            return {"success": False, "operation": operation, "error": "删除操作被禁止"}

        if op_type == "read_file":
            return self._read_file(path)
        elif op_type == "append_file":
            return self._append_file(path, content)
        elif op_type == "write_file":
            return self._write_file(path, content)
        elif op_type == "create_file":
            return self._create_file(path, content)
        elif op_type == "patch_file":
            return self._patch_file(path, content)
        elif op_type == "create_directory":
            return self._create_directory(path)
        elif op_type == "create_issue":
            return self._create_issue(path, content)
        elif op_type == "update_registry":
            return self._update_registry(path, content)
        elif op_type == "update_self_model":
            return self._update_self_model(content)
        elif op_type == "update_changelog":
            return self._update_changelog(content)
        elif op_type == "run_tests":
            return {"success": True, "message": "测试运行已安排"}
        else:
            return {"success": False, "operation": operation, "error": f"未知操作类型: {op_type}"}

    def _read_file(self, path: str) -> dict[str, Any]:
        """读取文件"""
        full_path = self.project_root / path

        if not full_path.exists():
            return {"success": False, "path": path, "error": "文件不存在"}

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            return {"success": True, "path": path, "action": "read", "content": content}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _append_file(self, path: str, content: str) -> dict[str, Any]:
        """追加文件"""
        full_path = self.project_root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path, "action": "append"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _write_file(self, path: str, content: str) -> dict[str, Any]:
        """写入文件（覆盖）"""
        full_path = self.project_root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path, "action": "write"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _create_file(self, path: str, content: str) -> dict[str, Any]:
        """创建文件"""
        full_path = self.project_root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if full_path.exists():
                return {"success": False, "path": path, "error": "文件已存在"}
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"success": True, "path": path, "action": "create"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _patch_file(self, path: str, patch_content: str) -> dict[str, Any]:
        """
        应用 patch。

        patch 格式：old_content -> new_content
        仅替换第一个匹配处，避免误替换。
        """
        full_path = self.project_root / path

        if not full_path.exists():
            return {"success": False, "path": path, "error": "文件不存在"}

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            if " -> " in patch_content:
                parts = patch_content.split(" -> ", 1)
                old_content = parts[0].strip()
                new_content = parts[1].strip()

                if old_content in original_content:
                    # 只替换第一个匹配处
                    idx = original_content.index(old_content)
                    patched_content = (
                        original_content[:idx]
                        + new_content
                        + original_content[idx + len(old_content):]
                    )
                else:
                    return {"success": False, "path": path, "error": "未找到要替换的内容"}
            else:
                # 非替换格式：追加到末尾
                patched_content = original_content + "\n" + patch_content

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(patched_content)

            return {"success": True, "path": path, "action": "patch"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _create_directory(self, path: str) -> dict[str, Any]:
        """创建目录"""
        full_path = self.project_root / path
        try:
            full_path.mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": path, "action": "create_directory"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _create_issue(self, path: str, content: str) -> dict[str, Any]:
        """创建 issue"""
        full_path = self.project_root / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if full_path.exists():
                with open(full_path, "a", encoding="utf-8") as f:
                    f.write("\n\n" + content)
            else:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
            return {"success": True, "path": path, "action": "create_issue"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    def _update_registry(self, path: str, content: str) -> dict[str, Any]:
        """更新注册表"""
        return self._append_file(path, content)

    def _update_self_model(self, content: str) -> dict[str, Any]:
        """更新自我模型"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n\n## {timestamp}\n\n{content}"
        return self._append_file("mind/self_model.md", entry)

    def _update_changelog(self, content: str) -> dict[str, Any]:
        """更新变更日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"\n\n## {timestamp}\n\n{content}"
        return self._append_file("self_development/changelog.md", entry)

    @timeout(10)
    def backup_file(self, path: str) -> dict[str, Any]:
        """备份文件"""
        full_path = self.project_root / path

        if not full_path.exists():
            return {"success": False, "path": path, "error": "文件不存在"}

        try:
            import shutil

            backup_dir = self.project_root / "releases" / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{Path(path).stem}_{timestamp}{Path(path).suffix}"
            backup_path = backup_dir / backup_name

            shutil.copy2(full_path, backup_path)

            return {
                "success": True,
                "path": path,
                "backup_path": str(backup_path.relative_to(self.project_root)),
            }
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}

    @timeout(10)
    def restore_file(self, path: str, backup_path: str) -> dict[str, Any]:
        """恢复文件"""
        full_path = self.project_root / path
        full_backup_path = self.project_root / backup_path

        if not full_backup_path.exists():
            return {"success": False, "path": path, "error": "备份文件不存在"}

        try:
            import shutil

            shutil.copy2(full_backup_path, full_path)
            return {"success": True, "path": path, "action": "restore"}
        except Exception as e:
            return {"success": False, "path": path, "error": str(e)}


# 全局实例
_update_executor: UpdateExecutor | None = None


def get_update_executor() -> UpdateExecutor:
    """获取全局更新执行器实例"""
    global _update_executor
    if _update_executor is None:
        _update_executor = UpdateExecutor()
    return _update_executor
