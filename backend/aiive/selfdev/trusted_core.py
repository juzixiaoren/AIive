"""SelfDev 补丁的仓库相对路径校验。"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


def normalize_repo_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        return "__invalid__"
    while raw.startswith("./"):
        raw = raw[2:]
    path = PurePosixPath(raw)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return "__invalid__"
    return path.as_posix()


def invalid_path_reason(value: str) -> str | None:
    normalized = normalize_repo_path(value)
    if normalized == "__invalid__":
        return "invalid_or_parent_traversal_path"
    return None


def validate_operations(operations: list[dict[str, Any]]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for operation in operations:
        target = str(operation.get("target_file") or "")
        reason = invalid_path_reason(target)
        if not target:
            reason = "missing_target_file"
        if reason:
            issues.append({"target_file": target, "reason": reason})
    return issues
