"""Local Bash execution and task process sessions."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, override

from aiive.context.run_context import RunContext
from aiive.tools.base import BaseTool, ToolExecutionContext
from aiive.tools.local_shell import run_bash, shell_timeout
from aiive.tools.shell_sessions import DEFAULT_WAIT_SECONDS, read_session
from aiive.tools.workspace_files import workspace_path


class RunShellTool(BaseTool):
    @override
    def _run(
        self, command: str, cwd: str = "", timeout_seconds: int = 0,
        env: dict[str, str | None] | None = None, background: bool = False,
        ctx: RunContext | None = None, _execution: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        workspace = None
        if ctx and ctx.task_workspace:
            workspace = workspace_path(ctx)
            cwd = str(workspace_path(ctx, cwd or "."))
            env = {
                "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
                "PYTHONPATH": str(workspace / "backend"),
                **ctx.workspace_env,
                **(env or {}),
            }
        if background:
            if not ctx or not ctx.task_workspace:
                raise ValueError("task_workspace_required")
            from aiive.tools.shell_sessions import start_session

            return start_session(ctx.task_id, command, cwd, shell_timeout(timeout_seconds), env or {})
        before = _stamp(workspace)
        result = run_bash(
            command, cwd=cwd, timeout_seconds=timeout_seconds, env=env,
            cancel_check=_cancel_check(_execution) if _execution else None,
        )
        if before is not None:
            after = _stamp(workspace)
            result["changed"] = after is not None and before != after
            if result["changed"]:
                result["side_effect_started"] = True
        return result


class ShellSessionTool(BaseTool):
    @override
    def _run(
        self, session_id: str, stdout_offset: int = 0, stderr_offset: int = 0,
        limit: int = 4000, cancel: bool = False, wait_seconds: int = DEFAULT_WAIT_SECONDS,
        ctx: RunContext | None = None, _execution: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        if not ctx or not ctx.task_id:
            raise ValueError("task_id_required")
        return read_session(
            ctx.task_id, session_id, stdout_offset, stderr_offset, limit, cancel,
            wait_seconds=max(1, wait_seconds) if ctx.task_workspace else wait_seconds,
            cancel_check=_cancel_check(_execution) if _execution else None,
        )


def _stamp(workspace: Path | None):
    if workspace is not None:
        from aiive.task_runtime.workspace import workspace_stamp

        try:
            return workspace_stamp(workspace)
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _cancel_check(execution: ToolExecutionContext):
    import time

    from aiive.tools.executor import cancellation_requested

    next_check = 0.0

    def check() -> bool:
        nonlocal next_check
        if execution.cancellation.requested:
            return True
        now = time.monotonic()
        if now < next_check:
            return False
        next_check = now + 1
        return cancellation_requested(execution)

    return check
