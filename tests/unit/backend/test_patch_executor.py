"""测试 PatchExecutor（补丁执行器）模块。

覆盖将补丁操作应用至非活跃槽位的功能，包括文件添加、操作跳过和活跃槽位隔离。
"""

from pathlib import Path

from aiive.selfdev.patch_executor import PatchExecutor
from aiive.supervisor.slot_manager import SlotManager


class TestPatchExecutor:
    """测试 PatchExecutor 的补丁应用操作。"""

    def test_apply_to_inactive_copies_manifest(self, tmp_path):
        """应用到非活跃槽位时应复制版本清单。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive([])
        assert result["ok"] is True
        assert result["active_slot"] == "A"
        assert result["inactive_slot"] == "B"

        inactive = tmp_path / "slots" / "B"
        assert (inactive / "version_manifest.json").exists()

    def test_apply_add_file_to_inactive(self, tmp_path):
        """添加文件操作应在非活跃槽位中创建目标文件。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "add_file",
                    "target_file": "new_module.py",
                    "content": "print('hello')",
                    "not_allowed_yet": False,
                }
            ],
        )
        assert result["ok"] is True
        new_file = tmp_path / "slots" / "B" / "app" / "new_module.py"
        assert new_file.exists()
        assert new_file.read_text() == "print('hello')"

    def test_not_allowed_yet_is_skipped(self, tmp_path):
        """标记为 not_allowed_yet 的操作应被跳过。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "add_file",
                    "target_file": "blocked.py",
                    "content": "bad",
                    "not_allowed_yet": True,
                }
            ],
        )
        # 存在失败（被跳过）操作时整体不应报告成功
        assert result["ok"] is False
        assert len(result["operations_applied"]) == 0
        assert len(result["operations_failed"]) == 1

    def test_active_slot_untouched(self, tmp_path):
        """补丁操作不应影响活跃槽位。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        # 先在活跃槽位中创建文件
        a_app = tmp_path / "slots" / "A" / "app"
        a_app.mkdir(parents=True, exist_ok=True)
        (a_app / "existing.py").write_text("original")

        executor = PatchExecutor(manager=mgr)
        executor.apply_to_inactive(
            [
                {
                    "operation": "add_file",
                    "target_file": "new_module.py",
                    "content": "new",
                    "not_allowed_yet": False,
                }
            ],
        )

        # 活跃槽位不受影响
        assert not (a_app / "new_module.py").exists()
        assert (a_app / "existing.py").read_text() == "original"

    def test_multiple_operations(self, tmp_path):
        """多个操作应全部被应用。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {"operation": "add_file", "target_file": "a.py", "content": "a", "not_allowed_yet": False},
                {"operation": "add_file", "target_file": "b.py", "content": "b", "not_allowed_yet": False},
            ],
        )
        assert len(result["operations_applied"]) == 2

    def test_delete_file_moves_to_trash(self, tmp_path):
        """delete_file 应走 safe_delete 入口，将文件移入回收站而非直接删除。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        # 基线来自活跃槽位：文件放在 A/app，apply 时同步到 B/app 后再删除
        a_app = tmp_path / "slots" / "A" / "app"
        a_app.mkdir(parents=True, exist_ok=True)
        (a_app / "obsolete.py").write_text("legacy")
        b_app = tmp_path / "slots" / "B" / "app"

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "delete_file",
                    "target_file": "obsolete.py",
                    "not_allowed_yet": False,
                }
            ]
        )
        assert result["ok"] is True
        assert len(result["operations_applied"]) == 1
        assert not (b_app / "obsolete.py").exists()
        assert (b_app / ".trash" / "obsolete.py").exists()

    def test_add_file_without_content_rejected(self, tmp_path):
        """add_file/modify_file 缺失 content 时应拒绝执行而非写空文件。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {"operation": "add_file", "target_file": "empty.py", "not_allowed_yet": False},
            ]
        )
        assert result["ok"] is False
        assert len(result["operations_failed"]) == 1
        assert "missing_content" in result["operations_failed"][0]["reason"]
        assert not (tmp_path / "slots" / "B" / "app" / "empty.py").exists()

    def test_absolute_target_path_rejected(self, tmp_path):
        """绝对路径 target_file 必须被拒绝，防止写仓库任意文件。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        outside = tmp_path / "evil.py"
        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "add_file",
                    "target_file": str(outside),
                    "content": "evil",
                    "not_allowed_yet": False,
                }
            ]
        )
        assert result["ok"] is False
        assert len(result["operations_applied"]) == 0
        assert "path_escape_denied" in result["operations_failed"][0]["reason"]
        assert not outside.exists()

    def test_traversal_target_path_rejected_for_add(self, tmp_path):
        """../ 穿越的 add_file 必须被拒绝。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "add_file",
                    "target_file": "../../A/app/injected.py",
                    "content": "evil",
                    "not_allowed_yet": False,
                }
            ]
        )
        assert result["ok"] is False
        assert "path_escape_denied" in result["operations_failed"][0]["reason"]
        assert not (tmp_path / "slots" / "A" / "app" / "injected.py").exists()

    def test_baseline_resynced_on_each_apply(self, tmp_path):
        """每次 apply 都应从活跃槽重刷基线，第二次 apply 不叠加上次残留。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        executor = PatchExecutor(manager=mgr)
        executor.apply_to_inactive(
            [{"operation": "add_file", "target_file": "first.py", "content": "1", "not_allowed_yet": False}]
        )
        assert (tmp_path / "slots" / "B" / "app" / "first.py").exists()

        executor.apply_to_inactive(
            [{"operation": "add_file", "target_file": "second.py", "content": "2", "not_allowed_yet": False}]
        )
        b_app = tmp_path / "slots" / "B" / "app"
        # 上一次 apply 的 first.py 不应残留（active 槽没有它）
        assert not (b_app / "first.py").exists()
        assert (b_app / "second.py").exists()

    def test_delete_file_rejects_path_traversal(self, tmp_path):
        """delete_file 必须拦截 ../ 路径穿越，保护槽位外文件。"""
        mgr = SlotManager(base_dir=tmp_path / "slots")
        mgr.init_slots()

        a_app = tmp_path / "slots" / "A" / "app"
        a_app.mkdir(parents=True, exist_ok=True)
        secret = a_app / "secret.py"
        secret.write_text("topsecret")

        executor = PatchExecutor(manager=mgr)
        result = executor.apply_to_inactive(
            [
                {
                    "operation": "delete_file",
                    "target_file": "../../A/app/secret.py",
                    "not_allowed_yet": False,
                }
            ]
        )
        # 越权删除被 safe_delete 的 scope 校验拒绝，文件应保持存在
        assert len(result["operations_applied"]) == 0
        assert secret.exists()
