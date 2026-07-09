import json
import shutil
from pathlib import Path

from aiive.supervisor.slot_manager import SlotManager


class PatchExecutor:
    def __init__(self, manager: SlotManager | None = None):
        self._manager = manager or SlotManager()

    def apply_to_inactive(
        self, operations: list[dict], base_dir: Path | None = None
    ) -> dict:
        """Copy active manifest to inactive, then apply operations."""
        slots = self._manager.list_slots()
        active_name = self._manager.get_active_slot()
        inactive_name = "B" if active_name == "A" else "A"

        active_slot = next((s for s in slots if s.name == active_name), None)
        inactive_slot = next((s for s in slots if s.name == inactive_name), None)
        if not active_slot or not inactive_slot:
            return {"ok": False, "error": "Slots not initialized"}

        root = base_dir or self._manager._base_dir

        # Copy manifest from active → inactive
        src_manifest = active_slot.root / "version_manifest.json"
        dst_manifest = inactive_slot.root / "version_manifest.json"
        if src_manifest.exists():
            dst_manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_manifest, dst_manifest)

        # Copy app directory
        src_app = active_slot.root / "app"
        dst_app = inactive_slot.root / "app"
        if src_app.exists() and not dst_app.exists():
            shutil.copytree(src_app, dst_app)

        # Apply operations
        results = []
        for op in operations:
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
                        target.unlink()
                        results.append({"ok": True, "file": str(target), "deleted": True})
                    else:
                        results.append({"ok": False, "file": str(target), "reason": "not found"})
                else:
                    results.append({"ok": False, "reason": f"Unknown op: {op_type}"})
            except Exception as e:
                results.append({"ok": False, "file": str(target), "error": str(e)})

        return {
            "ok": True,
            "active_slot": active_name,
            "inactive_slot": inactive_name,
            "operations_applied": [r for r in results if r["ok"]],
            "operations_failed": [r for r in results if not r["ok"]],
        }
