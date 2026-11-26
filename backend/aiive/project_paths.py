from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any


def source_root() -> Path:
    """Resolve the checkout from configuration or project markers at the launch cwd."""
    configured = os.environ.get("AIIVE_SOURCE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for root in (cwd, *cwd.parents):
        if root.name == "app" and root.parent.name in {"A", "B"} and root.parent.parent.name == "slots":
            return root.parent.parent.parent
        if (root / "pyproject.toml").is_file() and (root / "backend" / "aiive").is_dir():
            return root
    return cwd


def slots_root() -> Path:
    return Path(os.environ.get("AIIVE_SLOTS_ROOT") or source_root() / "slots").expanduser().resolve()


def runtime_root() -> Path:
    return Path(os.environ.get("AIIVE_RUNTIME_DIR") or slots_root().parent / "runtime").expanduser().resolve()


def runtime_identity() -> dict[str, Any]:
    from aiive.runtime.system_contract import stable_contract_audit
    from aiive.supervisor.slot_manager import SlotManager

    package = find_spec("aiive")
    module = find_spec("aiive.runtime.system_contract")
    source = source_root()
    package_path = Path(package.origin).resolve() if package and package.origin else None
    app_root = package_path.parents[2] if package_path and package_path.parent.parent.name == "backend" else None
    manager = SlotManager()
    try:
        pointer = (manager.runtime_dir / "active_slot").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        pointer = None
    active = pointer if pointer in {"A", "B"} else None
    actual_slot = next(
        (slot for slot in ("A", "B") if app_root == (manager.base_dir / slot / "app").resolve()),
        None,
    )
    matches = actual_slot == active if actual_slot and active else None
    deployment_state = "unknown"
    if matches is not None:
        deployment_state = "loaded_active_slot" if matches else "restart_required"
    elif app_root == source and not os.environ.get("AIIVE_SLOT"):
        deployment_state = "standalone_source"
    managed = os.environ.get("AIIVE_MANAGED_BY_SUPERVISOR") == "1"
    slot_version = os.environ.get("AIIVE_LOADED_SLOT_VERSION") if actual_slot == os.environ.get("AIIVE_SLOT") else None
    slot_revision = os.environ.get("AIIVE_LOADED_SLOT_REVISION") if slot_version else None
    try:
        package_version = version("aiive")
    except PackageNotFoundError:
        package_version = None
    return {
        "canonical_source_root": str(source),
        "mode": "supervisor" if managed else "standalone",
        "loaded_slot": actual_slot,
        "runtime_app_root": str(app_root) if app_root else None,
        "active_slot_pointer": active,
        "loaded_slot_matches_active": matches,
        "deployment_state": deployment_state,
        "package_version": package_version,
        "loaded_slot_version": slot_version,
        "loaded_slot_revision": slot_revision,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "python": sys.executable,
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "python_module_search_path": list(sys.path),
        "package_path": package.origin if package else None,
        "system_contract_module": module.origin if module else None,
        **stable_contract_audit(),
    }


def slot_environment(app: Path, slot: str, *, slots: Path, runtime: Path) -> dict[str, str]:
    from aiive.storage.data_paths import data_root
    from aiive.supervisor.slot_manager import SlotManager

    manifest = SlotManager(base_dir=slots).read_manifest(app.parent)
    return {
        "AIIVE_SOURCE_ROOT": str(source_root()),
        "AIIVE_ENV_FILE": os.environ.get("AIIVE_ENV_FILE") or str(source_root() / ".env"),
        "AIIVE_SLOTS_ROOT": str(slots.resolve()),
        "AIIVE_RUNTIME_DIR": str(runtime.resolve()),
        "AIIVE_RUNTIME_APP_ROOT": str(app.resolve()),
        "AIIVE_DATA_DIR": str(data_root()),
        "AIIVE_SLOT": slot,
        "AIIVE_LOADED_SLOT_VERSION": str(manifest.get("version") or ""),
        "AIIVE_LOADED_SLOT_REVISION": str(manifest.get("workspace_hash") or ""),
        "PYTHONPATH": str((app / "backend").resolve()),
    }
