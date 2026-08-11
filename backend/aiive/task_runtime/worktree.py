"""显式请求时为代码任务创建隔离 Git worktree。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


class WorktreeIsolation:
    """只在 Task Scope ``isolation_mode=git_worktree`` 时由运行时显式调用。"""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != self.repo_root:
            raise ValueError("workspace_is_not_git_repository")

    def path_for(self, task_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9-]", "", task_id)[:36]
        if not safe:
            raise ValueError("invalid_task_id")
        return self.repo_root / ".data" / "task-worktrees" / safe

    def create(self, task_id: str, base_ref: str = "HEAD") -> Path:
        destination = self.path_for(task_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return destination
        branch = f"aiive/task-{task_id[:12]}"
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(destination), base_ref],
            cwd=self.repo_root,
            check=True,
            capture_output=True,
            text=True,
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
        )
