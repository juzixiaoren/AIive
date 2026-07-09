"""
安全删除模块：提供可控、安全的文件删除操作。
仅允许删除注册的 scope 范围内的文件，禁用符号链接、保护危险路径。
支持三种模式：trash（回收站）、quarantine（隔离）、hard_delete_for_test_only（仅测试用）。
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SafeDeleteScopeRegistry:
    """安全删除范围注册表，管理允许删除的目录范围。

    只有注册过的 scope 目录下的文件才允许被删除。
    """

    def __init__(self):
        """初始化空的范围注册表。"""
        self._scopes: dict[str, Path] = {}

    def register(self, scope_id: str, root: Path) -> None:
        """注册一个允许删除的范围。

        参数:
            scope_id: 范围标识符，如 "test_artifacts"
            root: 范围的根目录路径
        """
        self._scopes[scope_id] = root.resolve()

    def get_scope_root(self, scope_id: str) -> Path | None:
        """获取指定范围的根目录。

        参数:
            scope_id: 范围标识符

        返回:
            根目录的 Path 对象或 None
        """
        return self._scopes.get(scope_id)

    def list_scopes(self) -> dict[str, str]:
        """列出所有已注册的范围。

        返回:
            {scope_id: 根目录字符串} 的字典
        """
        return {k: str(v) for k, v in self._scopes.items()}


# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


def _build_default_registry() -> SafeDeleteScopeRegistry:
    """构建默认的范围注册表，注册安全的删除范围。

    默认注册四个范围：
    - test_sandbox: 测试沙箱目录
    - test_artifacts: 测试产物目录
    - inactive_slot_placeholder: 非活跃槽占位目录
    - object_store: 对象存储目录
    """
    registry = SafeDeleteScopeRegistry()
    registry.register("test_sandbox", REPO_ROOT / "tests" / "artifacts")
    registry.register("test_artifacts", REPO_ROOT / "tests" / "artifacts")
    registry.register("inactive_slot_placeholder", REPO_ROOT / "tests" / "artifacts" / "slot_placeholder")
    registry.register("object_store", REPO_ROOT / ".data" / "object_store")
    return registry


# 全局单例
_default_registry: SafeDeleteScopeRegistry | None = None


def get_scope_registry() -> SafeDeleteScopeRegistry:
    """获取全局安全删除范围注册表单例。

    首次调用时自动初始化默认范围。

    返回:
        SafeDeleteScopeRegistry 全局单例
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = _build_default_registry()
    return _default_registry


# 危险路径集合：这些路径及其子路径不允许删除
DANGEROUS_PATHS = frozenset({
    "/",
    str(Path.home()),
    str(REPO_ROOT),
    str(REPO_ROOT / "backend"),
})


@dataclass
class DeleteDecision:
    """删除决策结果。

    属性:
        allowed: 是否允许删除
        reason: 拒绝原因或执行模式描述
        resolved_path: 解析后的绝对路径
        scope_id: 范围标识符
        mode: 删除模式
    """
    allowed: bool
    reason: str = ""
    resolved_path: str = ""
    scope_id: str = ""
    mode: str = ""


def safe_delete(
    path: str,
    scope_id: str,
    mode: str = "trash",
    registry: SafeDeleteScopeRegistry | None = None,
) -> DeleteDecision:
    """安全删除操作的唯一入口函数。

    执行多层安全检查：
    1. 符号链接检查：拒绝符号链接
    2. 路径解析：解析为绝对路径
    3. 危险路径检查：拒绝危险路径及其子路径
    4. 范围检查：目标必须在注册的 scope 根目录下
    5. 存在性检查：路径必须存在
    6. 执行删除：根据 mode 选择策略

    支持的模式：
    - trash: 移动到 .trash 子目录（可恢复）
    - quarantine: 移动到 .quarantine 子目录（隔离）
    - hard_delete_for_test_only: 永久删除（仅限测试）

    参数:
        path: 要删除的路径
        scope_id: 范围标识符
        mode: 删除模式，默认为 trash
        registry: 范围注册表，默认使用全局单例

    返回:
        DeleteDecision 决策结果
    """
    if registry is None:
        registry = get_scope_registry()

    # 符号链接检查：必须在 resolve 之前检查
    raw_path = Path(path)
    if raw_path.is_symlink():
        return DeleteDecision(
            allowed=False,
            reason="Symlinks are not allowed for deletion",
            resolved_path=str(raw_path),
        )

    # 解析为绝对路径
    try:
        resolved = raw_path.resolve()
    except Exception:
        logger.warning("路径解析失败: path=%s", path, exc_info=True)
        return DeleteDecision(
            allowed=False,
            reason="Cannot resolve path",
            resolved_path=path,
        )

    resolved_str = str(resolved)

    # 拒绝危险路径
    for dangerous in DANGEROUS_PATHS:
        dangerous_resolved = str(Path(dangerous).resolve())
        if resolved_str == dangerous_resolved or resolved_str.startswith(
            dangerous_resolved + os.sep
        ):
            return DeleteDecision(
                allowed=False,
                reason=f"Path is within dangerous area: {dangerous}",
                resolved_path=resolved_str,
            )

    # 检查 scope 范围
    scope_root = registry.get_scope_root(scope_id)
    if scope_root is None:
        return DeleteDecision(
            allowed=False,
            reason=f"Unknown scope: {scope_id}",
            resolved_path=resolved_str,
        )

    if not resolved_str.startswith(str(scope_root)):
        return DeleteDecision(
            allowed=False,
            reason=f"Path outside scope '{scope_id}': {scope_root}",
            resolved_path=resolved_str,
            scope_id=scope_id,
        )

    # 存在性检查
    if not resolved.exists():
        return DeleteDecision(
            allowed=False,
            reason="Path does not exist",
            resolved_path=resolved_str,
            scope_id=scope_id,
            mode=mode,
        )

    # 执行删除
    try:
        if mode == "trash":
            # 移动到 .trash 目录（文件名冲突时追加时间戳）
            trash_dir = resolved.parent / ".trash"
            trash_dir.mkdir(exist_ok=True)
            dest = trash_dir / resolved.name
            if dest.exists():
                dest = trash_dir / f"{resolved.name}.{_timestamp()}"
            shutil.move(str(resolved), str(dest))
        elif mode == "quarantine":
            # 移动到 .quarantine 目录
            quarantine_dir = resolved.parent / ".quarantine"
            quarantine_dir.mkdir(exist_ok=True)
            dest = quarantine_dir / resolved.name
            if dest.exists():
                dest = quarantine_dir / f"{resolved.name}.{_timestamp()}"
            shutil.move(str(resolved), str(dest))
        elif mode == "hard_delete_for_test_only":
            # 永久删除（仅限测试环境）
            if resolved.is_dir():
                shutil.rmtree(str(resolved))
            else:
                resolved.unlink()
        else:
            return DeleteDecision(
                allowed=False,
                reason=f"Unknown mode: {mode}",
                resolved_path=resolved_str,
            )

        return DeleteDecision(
            allowed=True,
            reason=f"Deleted with mode={mode}",
            resolved_path=resolved_str,
            scope_id=scope_id,
            mode=mode,
        )
    except Exception as e:
        logger.error("文件删除失败: path=%s mode=%s", resolved_str, mode, exc_info=True)
        return DeleteDecision(
            allowed=False,
            reason=f"Delete failed: {e}",
            resolved_path=resolved_str,
        )


def _timestamp() -> str:
    """生成当前 Unix 时间戳字符串，用于文件重命名避免冲突。"""
    import time
    return str(int(time.time()))
