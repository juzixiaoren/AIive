"""显式请求时为代码任务创建隔离 Git worktree。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 30


class WorktreeIsolation:
    """只在 Task Scope ``isolation_mode=git_worktree`` 时由运行时显式调用。"""

    def __init__(self, repo_root: Path):
        self.repo_root: Path = repo_root.resolve()
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != self.repo_root:
            raise ValueError("workspace_is_not_git_repository")

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9-]", "", task_id)[:36]
        if not safe:
            raise ValueError("invalid_task_id")
        return safe

    def path_for(self, task_id: str) -> Path:
        safe = self._safe_task_id(task_id)
        return self.repo_root / ".data" / "task-worktrees" / safe

    def _is_registered_worktree(self, destination: Path) -> bool:
        if not destination.is_dir():
            return False
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=destination,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return (
            probe.returncode == 0
            and bool(probe.stdout.strip())
            and Path(probe.stdout.strip()).resolve() == destination.resolve()
        )

    def create(self, task_id: str, base_ref: str = "HEAD") -> Path:
        destination = self.path_for(task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self._is_registered_worktree(destination):
                return destination
            raise RuntimeError("worktree_destination_conflict")

        safe_task_id = self._safe_task_id(task_id)
        branch = f"aiive/task-{safe_task_id}"
        branch_ref = f"refs/heads/{branch}"
        branch_probe = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", branch_ref],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if branch_probe.returncode not in (0, 1):
            raise subprocess.CalledProcessError(
                branch_probe.returncode,
                branch_probe.args,
                output=branch_probe.stdout,
                stderr=branch_probe.stderr,
            )
        command = ["git", "worktree", "add"]
        if branch_probe.returncode == 1:
            command.extend(["-b", branch])
        command.extend([str(destination), branch if branch_probe.returncode == 0 else base_ref])
        subprocess.run(
            command,
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return destination

    def remove(self, task_id: str) -> None:
        destination = self.path_for(task_id)
        if not destination.exists():
            return
        subprocess.run(
            ["git", "worktree", "remove", str(destination)],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
