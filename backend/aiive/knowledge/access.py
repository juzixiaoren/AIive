"""知识文档本地路径的统一最小权限边界。"""
from __future__ import annotations

import os
from pathlib import Path

from aiive.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[3]


def allowed_knowledge_roots() -> tuple[Path, ...]:
    # 经 Settings 读取，确保写在 .env（而非仅 export 到进程环境）的配置生效。
    raw = settings.aiive_knowledge_roots
    roots = tuple(
        Path(part.strip()).expanduser().resolve()
        for part in raw.split(os.pathsep)
        if part.strip()
    )
    if roots:
        return roots
    # 开箱即用地支持用户文档，同时保留应用自己的导入投递目录。
    return (
        (Path.home() / "Documents").resolve(),
        (_REPO_ROOT / ".data" / "knowledge").resolve(),
    )


def resolve_knowledge_path(file_path: str) -> Path:
    roots = allowed_knowledge_roots()
    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    allowed = os.pathsep.join(str(root) for root in roots)
    raise ValueError(f"knowledge_path_outside_allowed_roots:{allowed}")
