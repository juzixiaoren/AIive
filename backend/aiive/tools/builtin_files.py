"""Domain tool handlers (implementation) split from builtin_tools."""

import logging

logger = logging.getLogger(__name__)

import os


def handle_safe_delete(path: str, scope_id: str = "test_artifacts", mode: str = "trash"):
    """安全删除文件，仅允许删除已注册 scope 内的文件。

    参数:
        path: 要删除的文件路径
        scope_id: 范围标识符，默认 "test_artifacts"
        mode: 删除模式（trash / quarantine / hard_delete_for_test_only）

    返回:
        包含 allowed、reason、resolved_path 的字典
    """
    from aiive.tools.safe_delete import safe_delete as do_safe_delete
    decision = do_safe_delete(path, scope_id, mode)
    # 关键：必须带 ok 键。operation_executor 依赖 ok/allowed 判定失败，
    # 否则删除被拒绝时会被记成 committed 成功（审计假成功）。
    return {
        "ok": decision.allowed,
        "allowed": decision.allowed,
        "reason": decision.reason,
        "resolved_path": decision.resolved_path,
    }

def handle_read_text_file(path: str, max_lines: int = 50):
    """读取 ~/Documents 目录下的文本文件（最多 max_lines 行）。

    只允许读取 ~/Documents 范围内的文件，拒绝其他路径。

    参数:
        path: 文件路径
        max_lines: 最大读取行数，默认 50

    返回:
        包含 lines 和 total_read 的字典，或包含 error 的错误字典
    """
    allowed_dir = os.path.realpath(os.path.expanduser("~/Documents"))
    real_path = os.path.realpath(path)
    # 用 normcase + commonpath 校验目录包含关系：
    # 避免裸 startswith 被兄弟目录（如 ~/Documents2）绕过，且兼容 Windows 大小写
    try:
        allowed_nc = os.path.normcase(allowed_dir)
        real_nc = os.path.normcase(real_path)
        within_allowed = os.path.commonpath([real_nc, allowed_nc]) == allowed_nc
    except ValueError:
        within_allowed = False
    if not within_allowed:
        return {"error": "Access denied", "path": path}
    if not os.path.isfile(real_path):
        return {"error": "File not found", "path": path}
    try:
        with open(real_path, "r") as f:
            lines = [line.rstrip("\n") for i, line in enumerate(f) if i < max_lines]
        return {"lines": lines, "total_read": len(lines)}
    except Exception as e:
        logger.warning("读取文本文件失败: path=%s, error=%s", real_path, e)
        return {"error": str(e)}
