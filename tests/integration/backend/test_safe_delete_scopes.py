"""safe_delete 范围与危险路径交互的回归测试。

回归背景：DANGEROUS_PATHS 含仓库根，且危险路径前缀检查曾先于 scope 检查执行，
导致所有默认注册的仓库内 scope（tests/artifacts、.data/object_store）被自己
封死，safe_delete 永远拒绝。修复后：已注册 scope 内的路径豁免危险前缀检查；
危险路径检查仍保护 scope 外的路径；危险路径本身（精确匹配）永远拒绝。
"""

import uuid
from pathlib import Path

import pytest

from aiive.tools.safe_delete import (
    REPO_ROOT,
    get_scope_registry,
    safe_delete,
)


class TestScopeExemptsDangerousPrefix:
    """已注册 scope 内的仓库路径应允许删除。"""

    def test_file_in_registered_repo_scope_is_deletable(self):
        """tests/artifacts 是默认注册 scope，且位于仓库内，应放行删除。"""
        artifacts = REPO_ROOT / "tests" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        target = artifacts / f"safe_delete_regression_{uuid.uuid4().hex}.txt"
        target.write_text("delete me")

        decision = safe_delete(
            str(target), "test_artifacts", "hard_delete_for_test_only"
        )
        assert decision.allowed is True, decision.reason
        assert not target.exists()

    def test_object_store_scope_is_deletable(self):
        """.data/object_store 是默认注册 scope，应放行删除（trash 模式）。"""
        store_root = REPO_ROOT / ".data" / "object_store"
        bucket = store_root / f"unit_test_bucket_{uuid.uuid4().hex}"
        bucket.mkdir(parents=True, exist_ok=True)
        target = bucket / "obj.bin"
        target.write_bytes(b"payload")

        try:
            decision = safe_delete(str(target), "object_store", "trash")
            assert decision.allowed is True, decision.reason
            assert not target.exists()
            assert (bucket / ".trash" / "obj.bin").exists()
        finally:
            import shutil

            shutil.rmtree(bucket, ignore_errors=True)


class TestDangerousPathsStillProtected:
    """scope 外的仓库路径与系统路径仍应被拒绝。"""

    def test_repo_file_outside_scope_denied(self):
        """仓库内但不在任何注册 scope 内的文件必须拒绝。"""
        target = REPO_ROOT / "README.md"
        decision = safe_delete(str(target), "test_artifacts", "trash")
        assert decision.allowed is False
        assert "dangerous" in decision.reason.lower()
        assert target.exists()

    def test_repo_root_itself_denied_even_with_scope(self):
        """危险路径本身（仓库根）永远拒绝，即使 scope 覆盖到它。"""
        registry = get_scope_registry()
        registry.register("evil_scope_repo_root", REPO_ROOT)
        try:
            decision = safe_delete(str(REPO_ROOT), "evil_scope_repo_root", "trash")
            assert decision.allowed is False
            assert "dangerous" in decision.reason.lower()
        finally:
            registry._scopes.pop("evil_scope_repo_root", None)

    def test_home_directory_denied(self):
        decision = safe_delete(str(Path.home()), "test_artifacts", "trash")
        assert decision.allowed is False

    def test_home_subpath_outside_scope_denied(self):
        decision = safe_delete(
            str(Path.home() / "nonexistent_probe_dir_xyz"), "test_artifacts", "trash"
        )
        assert decision.allowed is False
        assert "dangerous" in decision.reason.lower()


class TestObjectStoreTmpCleanup:
    """object_store._atomic_put 失败路径的 tmp 清理依赖 safe_delete 放行。"""

    def test_atomic_put_failure_cleans_tmp(self, monkeypatch):
        import os as _os
        import shutil

        from aiive.storage import object_store

        bucket_name = f"unit_test_cleanup_{uuid.uuid4().hex}"
        bucket_dir = object_store.ROOT / bucket_name

        def _boom(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(object_store.os, "replace", _boom)
        try:
            with pytest.raises(OSError):
                object_store.put_bytes(bucket_name, "k.bin", b"data")
            # tmp 文件必须被 safe_delete 移走（不残留在对象目录）
            leftovers = [
                p for p in bucket_dir.iterdir()
                if p.is_file() and p.name.endswith(".tmp")
            ]
            assert leftovers == []
        finally:
            monkeypatch.undo()
            shutil.rmtree(bucket_dir, ignore_errors=True)
