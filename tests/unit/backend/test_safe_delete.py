import os
from pathlib import Path

import pytest

from aiive.tools.safe_delete import (
    DANGEROUS_PATHS,
    DeleteDecision,
    SafeDeleteScopeRegistry,
    safe_delete,
)


class TestSafeDeleteScopeRegistry:
    def test_register_and_get(self):
        registry = SafeDeleteScopeRegistry()
        registry.register("test", Path("/tmp/test_area"))
        assert registry.get_scope_root("test") == Path("/tmp/test_area").resolve()

    def test_unknown_scope_returns_none(self):
        registry = SafeDeleteScopeRegistry()
        assert registry.get_scope_root("unknown") is None

    def test_list_scopes(self, tmp_path):
        registry = SafeDeleteScopeRegistry()
        registry.register("s1", tmp_path / "a")
        registry.register("s2", tmp_path / "b")
        scopes = registry.list_scopes()
        assert "s1" in scopes
        assert "s2" in scopes


class TestSafeDelete:
    def test_trash_mode_moves_file(self, tmp_path):
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
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "temp.txt"
        test_file.write_text("gone")

        decision = safe_delete(str(test_file), "sandbox", "hard_delete_for_test_only", registry)
        assert decision.allowed is True
        assert not test_file.exists()
        # No .trash or .quarantine created
        assert not (scope_dir / ".trash").exists()

    def test_hard_delete_removes_directory(self, tmp_path):
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
        decision = safe_delete("/", "test_sandbox", "trash")
        assert decision.allowed is False
        assert "dangerous" in decision.reason.lower()

    def test_denies_home_directory(self, tmp_path):
        registry = SafeDeleteScopeRegistry()
        registry.register("sandbox", tmp_path / "sandbox")
        decision = safe_delete(str(Path.home()), "sandbox", "trash", registry)
        assert decision.allowed is False

    def test_denies_outside_scope(self, tmp_path):
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
        decision = safe_delete(str(tmp_path / "x.txt"), "nonexistent", "trash")
        assert decision.allowed is False
        assert "unknown scope" in decision.reason.lower()

    def test_denies_symlink(self, tmp_path):
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
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        decision = safe_delete(str(scope_dir / "ghost.txt"), "sandbox", "trash", registry)
        assert decision.allowed is False
        assert "does not exist" in decision.reason.lower()

    def test_unknown_mode_denied(self, tmp_path):
        registry = SafeDeleteScopeRegistry()
        scope_dir = tmp_path / "sandbox"
        scope_dir.mkdir()
        registry.register("sandbox", scope_dir)

        test_file = scope_dir / "x.txt"
        test_file.write_text("x")

        decision = safe_delete(str(test_file), "sandbox", "invalid_mode", registry)
        assert decision.allowed is False
        assert "unknown mode" in decision.reason.lower()

    def test_denies_repo_root(self):
        decision = safe_delete(str(Path(__file__).resolve().parent.parent.parent.parent.parent), "test_sandbox", "trash")
        # Should be denied because it's the repo root (or outside scope)
        assert decision.allowed is False
