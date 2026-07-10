"""
补丁执行模块。

负责将 SelfDevPlanner 生成的补丁计划应用到非活跃槽位（inactive slot）。
支持三种操作：add_file（新增文件）、modify_file（修改文件）、delete_file（删除文件）。
执行前会从活跃槽位复制 manifest 和 app 目录到非活跃槽位，确保变更隔离。
"""

import logging
import shutil

from aiive.supervisor.slot_manager import SlotManager
from aiive.tools.safe_delete import get_scope_registry, safe_delete
from typing import Any

logger = logging.getLogger(__name__)


class PatchExecutor:
    """补丁执行器，将操作列表应用到非活跃槽位。"""

    def __init__(self, manager: SlotManager | None = None):
        """
        初始化补丁执行器。

        参数:
            manager: 槽位管理器实例，默认自动创建。
        """
        self._manager: SlotManager = manager or SlotManager()

    def apply_to_inactive(
        self, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """
        将活跃槽位的 manifest 和 app 目录复制到非活跃槽位，然后应用操作列表。

        参数:
            operations: 操作列表，每项包含 operation、target_file、content 等字段。

        返回:
            包含 active_slot、inactive_slot、operations_applied、
            operations_failed 的结果字典。
        """
        slots = self._manager.list_slots()
        active_name = self._manager.get_active_slot()
        inactive_name = "B" if active_name == "A" else "A"

        active_slot = next((s for s in slots if s.name == active_name), None)
        inactive_slot = next((s for s in slots if s.name == inactive_name), None)
        if not active_slot or not inactive_slot:
            return {"ok": False, "error": "Slots not initialized"}

        # 第一步：从活跃槽位复制 manifest 到非活跃槽位
        src_manifest = active_slot.root / "version_manifest.json"
        dst_manifest = inactive_slot.root / "version_manifest.json"
        if src_manifest.exists():
            dst_manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_manifest, dst_manifest)

        # 第二步：复制 app 目录（仅在目标不存在时，避免覆盖已修改内容）
        src_app = active_slot.root / "app"
        dst_app = inactive_slot.root / "app"
        if src_app.exists() and not dst_app.exists():
            shutil.copytree(src_app, dst_app)

        # 第三步：逐一应用操作
        results = []
        for op in operations:
            # 跳过标记为不可执行的操作
            if op.get("not_allowed_yet"):
                results.append({"ok": False, "reason": "not_allowed_yet", "op": op})
                continue

            target = dst_app / op["target_file"]
            op_type = op.get("operation", "add_file")

            try:
                if op_type == "add_file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(op.get("content", ""))
                    results.append({"ok": True, "file": str(target)})
                elif op_type == "modify_file":
                    if target.exists():
                        content = op.get("content", "")
                        target.write_text(content)
                        results.append({"ok": True, "file": str(target)})
                    else:
                        results.append({"ok": False, "file": str(target), "reason": "not found"})
                elif op_type == "delete_file":
                    if target.exists():
                        # 删除必须走 safe_delete 唯一入口：scope 仅放行 inactive
                        # 槽位的 app 目录，可拦截 ../ 路径穿越与符号链接删除。
                        registry = get_scope_registry()
                        scope_id = f"inactive_slot_app_{inactive_name}"
                        registry.register(scope_id, inactive_slot.root / "app")
                        decision = safe_delete(
                            str(target), scope_id, mode="trash", registry=registry
                        )
                        if decision.allowed:
                            results.append(
                                {"ok": True, "file": str(target), "deleted": True}
                            )
                        else:
                            results.append(
                                {
                                    "ok": False,
                                    "file": str(target),
                                    "reason": decision.reason,
                                }
                            )
                    else:
                        results.append({"ok": False, "file": str(target), "reason": "not found"})
                else:
                    results.append({"ok": False, "reason": f"Unknown op: {op_type}"})
            except Exception as e:
                logger.error("补丁操作失败: target=%s op_type=%s", str(target), op_type, exc_info=True)
                results.append({"ok": False, "file": str(target), "error": str(e)})

        return {
            "ok": True,
            "active_slot": active_name,
            "inactive_slot": inactive_name,
            "operations_applied": [r for r in results if r["ok"]],
            "operations_failed": [r for r in results if not r["ok"]],
        }
