"""不可由 Self-Improvement Task 修改的 Trusted Core 边界。"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


TRUSTED_CORE_PREFIXES = (
    "backend/aiive/main.py",
    "backend/aiive/config.py",
    "backend/aiive/db/",
    "backend/aiive/control/",
    "backend/aiive/task_runtime/",
    "backend/aiive/api/routes_approval.py",
    "backend/aiive/api/routes_selfdev.py",
    "backend/aiive/selfdev/trusted_core.py",
    "backend/aiive/supervisor/",
    "backend/aiive/prompts/manifest.toml",
    "backend/alembic/",
    ".github/",
    "alembic.ini",
)


def normalize_repo_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:/", raw):
        return "__invalid__"
    while raw.startswith("./"):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return "__invalid__"
    return path.as_posix()


def protected_reason(value: str) -> str | None:
    normalized = normalize_repo_path(value)
    if normalized == "__invalid__":
        return "invalid_or_parent_traversal_path"
    for prefix in TRUSTED_CORE_PREFIXES:
        if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
            return f"trusted_core_protected:{prefix}"
    return None


def validate_operations(operations: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for operation in operations:
        target = str(operation.get("target_file") or "")
        reason = protected_reason(target)
        if not target:
            reason = "missing_target_file"
        if reason:
            issues.append({"target_file": target, "reason": reason})
    return issues
