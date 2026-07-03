"""
Version Manager模块

管理工作区版本，实现 candidate-promote-rollback 流程。
"""

import shutil
from pathlib import Path
from datetime import datetime
from typing import Any

from core.utils import timeout


class VersionManager:
    """版本管理器"""

    # 需要纳入版本管理的核心目录
    CORE_DIRS: list[str] = [
        "core",
        "kernel",
        "mind",
        "memory",
        "capabilities",
        "self_development",
        "docs",
        "tests",
    ]

    # 需要纳入版本管理的核心文件
    CORE_FILES: list[str] = [
        "main.py",
        "README.md",
    ]

    def __init__(self, project_root: str | None = None) -> None:
        self.project_root: Path
        if project_root:
            self.project_root = Path(project_root)
        else:
            self.project_root = Path(__file__).parent.parent

        self.releases_dir: Path = self.project_root / "releases"
        self.candidate_dir: Path = self.releases_dir / "candidate"
        self.previous_dir: Path = self.releases_dir / "previous"
        self.logs_dir: Path = self.releases_dir / "logs"

        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """确保运行时目录存在"""
        self.releases_dir.mkdir(exist_ok=True)
        self.candidate_dir.mkdir(exist_ok=True)
        self.previous_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

    @timeout(30)
    def create_candidate(self) -> dict[str, Any]:
        """
        创建 candidate 工作区，从当前项目复制核心代码。

        Returns:
            创建结果
        """
        try:
            if self.candidate_dir.exists():
                shutil.rmtree(self.candidate_dir)
            self.candidate_dir.mkdir(parents=True)

            for dir_name in self.CORE_DIRS:
                src_dir = self.project_root / dir_name
                dst_dir = self.candidate_dir / dir_name
                if src_dir.exists():
                    _ = shutil.copytree(
                        src_dir,
                        dst_dir,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
                    )

            for file_name in self.CORE_FILES:
                src_file = self.project_root / file_name
                dst_file = self.candidate_dir / file_name
                if src_file.exists():
                    _ = shutil.copy2(src_file, dst_file)

            self._log("create_candidate", "成功创建 candidate 工作区")
            return {
                "success": True,
                "message": "成功创建 candidate 工作区",
                "candidate_path": str(self.candidate_dir),
            }
        except Exception as e:
            self._log("create_candidate", f"失败: {e}", level="ERROR")
            return {"success": False, "message": f"创建 candidate 失败: {e}"}

    @timeout(30)
    def apply_operations_to_candidate(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """
        在 candidate 中应用操作。

        Args:
            operations: 操作列表

        Returns:
            执行结果
        """
        if not self.candidate_dir.exists():
            return {"success": False, "message": "candidate 工作区不存在，请先创建"}

        from core.update_executor import UpdateExecutor

        executor = UpdateExecutor(str(self.candidate_dir))
        result = executor.execute_operations(operations)

        self._log("apply_operations", f"在 candidate 中执行了 {len(operations)} 个操作")
        return result

    def run_candidate_tests(
        self,
        test_cmd: list[str] | None = None,
        timeout_seconds: int = 240,
    ) -> dict[str, Any]:
        """
        在 candidate 中运行测试。

        Args:
            test_cmd: 测试命令列表，默认 ["python3", "-m", "pytest", "tests/", "-v"]
            timeout_seconds: 超时时间（秒），默认 240

        Returns:
            测试结果
        """
        if test_cmd is None:
            test_cmd = ["python3", "-m", "pytest", "tests/", "-v"]

        if not self.candidate_dir.exists():
            return {"success": False, "message": "candidate 工作区不存在"}

        try:
            import subprocess
            import sys
            import threading

            process = subprocess.Popen(
                test_cmd,
                cwd=str(self.candidate_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def read_stdout() -> None:
                assert process.stdout is not None
                while True:
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end="", flush=True)
                        stdout_lines.append(line)

            def read_stderr() -> None:
                assert process.stderr is not None
                while True:
                    line = process.stderr.readline()
                    if not line and process.poll() is not None:
                        break
                    if line:
                        print(line, end="", file=sys.stderr, flush=True)
                        stderr_lines.append(line)

            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            timed_out = threading.Event()

            def timeout_handler() -> None:
                timed_out.set()
                process.terminate()

            timer = threading.Timer(timeout_seconds, timeout_handler)
            timer.start()

            try:
                returncode = process.wait()
            finally:
                timer.cancel()
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)

            if timed_out.is_set():
                print(f"[VersionManager] 测试超时（超过 {timeout_seconds} 秒）")
                return {"success": False, "message": f"测试超时（超过 {timeout_seconds} 秒）"}

            success = returncode == 0
            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)

            print(f"[VersionManager] 测试{'通过' if success else '失败'}")
            return {
                "success": success,
                "returncode": returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "message": "测试通过" if success else "测试失败",
            }
        except Exception as e:
            print(f"[VersionManager] 测试执行失败: {e}")
            return {"success": False, "message": f"测试执行失败: {e}"}

    @timeout(30)
    def promote_candidate(self) -> dict[str, Any]:
        """
        将 candidate 提升为 current（带回退保护）。

        流程：
        1. 备份当前版本到 previous/
        2. 将 candidate 复制到项目根目录
        3. 如果中途失败，从 previous/ 恢复

        Returns:
            提升结果
        """
        if not self.candidate_dir.exists():
            return {"success": False, "message": "candidate 工作区不存在"}

        backup_dir: Path | None = None

        try:
            # 第一步：备份当前版本到 previous/
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_dir = self.previous_dir / timestamp
            backup_dir.mkdir(parents=True)

            for dir_name in self.CORE_DIRS:
                src_dir = self.project_root / dir_name
                dst_dir = backup_dir / dir_name
                if src_dir.exists():
                    _ = shutil.copytree(src_dir, dst_dir)

            for file_name in self.CORE_FILES:
                src_file = self.project_root / file_name
                dst_file = backup_dir / file_name
                if src_file.exists():
                    _ = shutil.copy2(src_file, dst_file)

            # 第二步：将 candidate 复制到当前项目
            for dir_name in self.CORE_DIRS:
                src_dir = self.candidate_dir / dir_name
                dst_dir = self.project_root / dir_name
                if src_dir.exists():
                    if dst_dir.exists():
                        shutil.rmtree(dst_dir)
                    _ = shutil.copytree(src_dir, dst_dir)

            for file_name in self.CORE_FILES:
                src_file = self.candidate_dir / file_name
                dst_file = self.project_root / file_name
                if src_file.exists():
                    _ = shutil.copy2(src_file, dst_file)

            # 第三步：清理 candidate
            shutil.rmtree(self.candidate_dir)

            self._log("promote_candidate", f"成功提升 candidate，备份保存到 {backup_dir}")
            return {
                "success": True,
                "message": "成功提升 candidate 为 current",
                "backup_path": str(backup_dir),
            }

        except Exception as e:
            # 回退：从备份恢复
            if backup_dir and backup_dir.exists():
                try:
                    self._restore_from_backup(backup_dir)
                    self._log(
                        "promote_candidate",
                        f"提升失败已回退: {e}",
                        level="ERROR",
                    )
                    return {
                        "success": False,
                        "message": f"提升失败，已从备份回退: {e}",
                    }
                except Exception as restore_error:
                    self._log(
                        "promote_candidate",
                        f"提升失败且回退也失败: {e}, 回退错误: {restore_error}",
                        level="ERROR",
                    )
                    return {
                        "success": False,
                        "message": f"提升失败且回退也失败: {e}",
                    }
            else:
                self._log("promote_candidate", f"提升失败: {e}", level="ERROR")
                return {"success": False, "message": f"提升 candidate 失败: {e}"}

    def _restore_from_backup(self, backup_dir: Path) -> None:
        """从备份目录恢复项目"""
        for dir_name in self.CORE_DIRS:
            src_dir = backup_dir / dir_name
            dst_dir = self.project_root / dir_name
            if src_dir.exists():
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                _ = shutil.copytree(src_dir, dst_dir)

        for file_name in self.CORE_FILES:
            src_file = backup_dir / file_name
            dst_file = self.project_root / file_name
            if src_file.exists():
                _ = shutil.copy2(src_file, dst_file)

    @timeout(10)
    def rollback_candidate(self) -> dict[str, Any]:
        """
        回滚 candidate（删除 candidate）。

        Returns:
            回滚结果
        """
        if not self.candidate_dir.exists():
            return {"success": False, "message": "candidate 工作区不存在"}

        try:
            shutil.rmtree(self.candidate_dir)
            self._log("rollback_candidate", "成功回滚 candidate")
            return {"success": True, "message": "成功回滚 candidate"}
        except Exception as e:
            self._log("rollback_candidate", f"回滚失败: {e}", level="ERROR")
            return {"success": False, "message": f"回滚 candidate 失败: {e}"}

    @timeout(30)
    def restore_previous_version(self, version_timestamp: str) -> dict[str, Any]:
        """
        恢复到之前的版本。

        Args:
            version_timestamp: 版本时间戳

        Returns:
            恢复结果
        """
        backup_dir = self.previous_dir / version_timestamp

        if not backup_dir.exists():
            return {"success": False, "message": f"备份版本 {version_timestamp} 不存在"}

        try:
            self._restore_from_backup(backup_dir)
            self._log("restore_previous", f"成功恢复到版本 {version_timestamp}")
            return {"success": True, "message": f"成功恢复到版本 {version_timestamp}"}
        except Exception as e:
            self._log("restore_previous", f"恢复失败: {e}", level="ERROR")
            return {"success": False, "message": f"恢复版本失败: {e}"}

    def list_previous_versions(self) -> list[str]:
        """
        列出之前的版本。

        Returns:
            版本时间戳列表（最新在前）
        """
        if not self.previous_dir.exists():
            return []

        return sorted(
            [d.name for d in self.previous_dir.iterdir() if d.is_dir()],
            reverse=True,
        )

    def get_status(self) -> dict[str, Any]:
        """获取版本状态"""
        return {
            "project_root": str(self.project_root),
            "candidate_exists": self.candidate_dir.exists(),
            "previous_versions": self.list_previous_versions(),
            "releases_dir": str(self.releases_dir),
        }

    def _log(self, action: str, message: str, level: str = "INFO") -> None:
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {action}: {message}\n"

        log_file = self.logs_dir / "version_manager.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                _ = f.write(log_entry)
        except Exception:
            pass


# 全局实例
_version_manager: VersionManager | None = None


def get_version_manager() -> VersionManager:
    """获取全局版本管理器实例"""
    global _version_manager
    if _version_manager is None:
        _version_manager = VersionManager()
    return _version_manager
