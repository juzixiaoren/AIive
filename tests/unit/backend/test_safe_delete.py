"""测试 SafeDelete（安全删除）模块。

覆盖文件删除的多种模式（trash/quarantine/hard_delete）及各类安全拦截规则。
"""

import os
from pathlib import Path

import pytest

from aiive.tools.safe_delete import (
    DANGEROUS_PATHS,
    DeleteDecision,
    REPO_ROOT,
    SafeDeleteScopeRegistry,
    safe_delete,
)


class TestRepoRoot:
    """回归测试：REPO_ROOT 必须正确指向仓库根（曾因多算一层 parent 而偏移）。"""

    def test_repo_root_is_project_root(self):
        """REPO_ROOT 应解析为包含 backend/aiive 的仓库根目录。"""
        assert REPO_ROOT.name == "AIive"
        assert (REPO_ROOT / "backend" / "aiive" / "tools" / "safe_delete.py").exists()

    def test_dangerous_paths_protect_real_repo(self):
        """危险路径集合必须覆盖真实仓库根与 backend，而非仓库外一级。"""
        assert str(REPO_ROOT) in DANGEROUS_PATHS
        assert str(REPO_ROOT / "backend") in DANGEROUS_PATHS
        # 仓库外一级目录绝不应被列为危险路径
        assert str(REPO_ROOT.parent) not in DANGEROUS_PATHS


class TestSafeDeleteScopeRegistry:
    """测试 SafeDeleteScopeRegistry 的范围注册和查询功能。"""

    def test_register_and_get(self):
        """注册范围后应能正确获取其根路径。"""
        registry = SafeDeleteScopeRegistry()
        registry.register("test", Path("/tmp/test_area"))
        assert registry.get_scope_root("test") == Path("/tmp/test_area").resolve()

    def test_unknown_scope_returns_none(self):
        """未知范围应返回 None。"""
        registry = SafeDeleteScopeRegistry()
        assert registry.get_scope_root("unknown") is None

    def test_list_scopes(self, tmp_path):
        """list_scopes 应返回所有已注册的范围名称。"""
        registry = SafeDeleteScopeRegistry()
        registry.register("s1", tmp_path / "a")
        registry.register("s2", tmp_path / "b")
        scopes = registry.list_scopes()
        assert "s1" in scopes
        assert "s2" in scopes


class TestSafeDelete:
    """测试 safe_delete 的各种删除模式和安全检查。"""

    def test_trash_mode_moves_file(self, tmp_path):
        """trash 模式应将文件移至 .trash 目录。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "test.txt"
        test_file.write_text("hello")

        decision = safe_delete(str(test_file), "sandbox", "trash", registry)
        assert decision.allowed is True
        assert decision.mode == "trash"
        assert not test_file.exists()

        trash_dir = scope_dir / ".trash"
        trash_files = list(trash_dir.iterdir())
        assert len(trash_files) == 1

    def test_quarantine_mode_moves_file(self, tmp_path):
        """quarantine 模式应将文件移至 .quarantine 目录隔离。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "data.txt"
        test_file.write_text("secret")

        decision = safe_delete(str(test_file), "sandbox", "quarantine", registry)
        assert decision.allowed is True
        assert not test_file.exists()

        quarantine_dir = scope_dir / ".quarantine"
        assert quarantine_dir.exists()

    def test_hard_delete_removes_file(self, tmp_path):
        """hard_delete 模式应直接删除文件，不创建 .trash 目录。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "temp.txt"
        test_file.write_text("gone")

        decision = safe_delete(str(test_file), "sandbox", "hard_delete_for_test_only", registry)
        assert decision.allowed is True
        assert not test_file.exists()
        # 不应创建 .trash 或 .quarantine
        assert not (scope_dir / ".trash").exists()

    def test_hard_delete_removes_directory(self, tmp_path):
        """hard_delete 模式应能删除整个目录。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_dir = scope_dir / "subdir"
        test_dir.mkdir()
        (test_dir / "f.txt").write_text("x")

        decision = safe_delete(str(test_dir), "sandbox", "hard_delete_for_test_only", registry)
        assert decision.allowed is True
        assert not test_dir.exists()

    def test_denies_root_path(self):
        """删除根路径 / 应被拒绝。"""
        decision = safe_delete("/", "test_sandbox", "trash")
        assert decision.allowed is False
        assert "dangerous" in decision.reason.lower()

    def test_denies_home_directory(self, tmp_path):
        """删除用户主目录应被拒绝。"""
        registry = SafeDeleteScopeRegistry()
        registry.register("sandbox", tmp_path / "sandbox")
        decision = safe_delete(str(Path.home()), "sandbox", "trash", registry)
        assert decision.allowed is False

    def test_denies_outside_scope(self, tmp_path):
        """超出注册范围的路径应被拒绝。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("no")

        decision = safe_delete(str(outside_file), "sandbox", "trash", registry)
        assert decision.allowed is False
        assert "outside scope" in decision.reason.lower()

    def test_denies_unknown_scope(self, tmp_path):
        """未知范围的删除应被拒绝。"""
        decision = safe_delete(str(tmp_path / "x.txt"), "nonexistent", "trash")
        assert decision.allowed is False
        assert "Any scope" in decision.reason

    def test_denies_symlink(self, tmp_path):
        """符号链接的删除应被拒绝。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        target = scope_dir / "real.txt"
        target.write_text("real")
        link = scope_dir / "link.txt"
        os.symlink(str(target), str(link))

        decision = safe_delete(str(link), "sandbox", "trash", registry)
        assert decision.allowed is False
        assert "symlink" in decision.reason.lower()

    def test_nonexistent_path(self, tmp_path):
        """不存在的路径应被拒绝。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        decision = safe_delete(str(scope_dir / "ghost.txt"), "sandbox", "trash", registry)
        assert decision.allowed is False
        assert "does not exist" in decision.reason.lower()

    def test_unknown_mode_denied(self, tmp_path):
        """未知的删除模式应被拒绝。"""
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "x.txt"
        test_file.write_text("x")

        decision = safe_delete(str(test_file), "sandbox", "invalid_mode", registry)
        assert decision.allowed is False
        assert "Any mode" in decision.reason

    def test_denies_repo_root(self, tmp_path):
        """仓库根目录的删除应被拒绝。"""
        decision = safe_delete(str(Path(__file__).resolve().parent.parent.parent.parent.parent), "test_sandbox", "trash")
        # 应为拒绝，因为它是仓库根目录（或超出范围）
        assert decision.allowed is False
