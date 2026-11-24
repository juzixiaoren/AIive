from __future__ import annotations

import os
import sys
from importlib.util import find_spec
from pathlib import Path


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


def runtime_identity() -> dict:
    from aiive.runtime.system_contract import stable_contract_audit

    package = find_spec("aiive")
    module = find_spec("aiive.runtime.system_contract")
    managed = os.environ.get("AIIVE_MANAGED_BY_SUPERVISOR") == "1"
    return {
        "canonical_source_root": str(source_root()),
        "mode": "supervisor" if managed else "standalone",
        "loaded_slot": os.environ.get("AIIVE_SLOT") or None,
        "runtime_app_root": os.environ.get("AIIVE_RUNTIME_APP_ROOT") or (
            str(source_root()) if package and package.origin and
            Path(package.origin).is_relative_to(source_root() / "backend") else None
        ),
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

    return {
        "AIIVE_SOURCE_ROOT": str(source_root()),
        "AIIVE_ENV_FILE": os.environ.get("AIIVE_ENV_FILE") or str(source_root() / ".env"),
        "AIIVE_SLOTS_ROOT": str(slots.resolve()),
        "AIIVE_RUNTIME_DIR": str(runtime.resolve()),
        "AIIVE_RUNTIME_APP_ROOT": str(app.resolve()),
        "AIIVE_DATA_DIR": str(data_root()),
        "AIIVE_SLOT": slot,
        "PYTHONPATH": str((app / "backend").resolve()),
    }
