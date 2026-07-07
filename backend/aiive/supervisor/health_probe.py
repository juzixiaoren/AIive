import os
from dataclasses import dataclass


@dataclass
class HealthResult:
    healthy: bool
    slot: str
    message: str = ""
    checks: list[dict] = None

    def __post_init__(self):
        if self.checks is None:
            self.checks = []


class HealthProbe:
    def check(self, slot: str, slot_root: str) -> HealthResult:
        checks: list[dict] = []
        healthy = True

        # Check manifest exists
        manifest_path = os.path.join(slot_root, "version_manifest.json")
        manifest_ok = os.path.exists(manifest_path)
        checks.append({"name": "manifest_exists", "ok": manifest_ok})
        if not manifest_ok:
            healthy = False

        # Check app directory exists
        app_dir = os.path.join(slot_root, "app")
        app_ok = os.path.isdir(app_dir)
        checks.append({"name": "app_dir_exists", "ok": app_ok})

        # Check backend imports work
        try:
            import importlib

            importlib.import_module("aiive.main")
            backend_ok = True
        except Exception:
            backend_ok = False
        checks.append({"name": "backend_import", "ok": backend_ok})

        return HealthResult(
            healthy=healthy,
            slot=slot,
            message="Healthy" if healthy else "Unhealthy",
            checks=checks,
        )
