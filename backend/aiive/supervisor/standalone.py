"""Start the standalone backend from the selected slot after an external restart."""
from __future__ import annotations

import os
import sys

from aiive.project_paths import slot_environment, source_root
from aiive.supervisor.slot_manager import SlotManager


def main() -> None:
    manager = SlotManager()
    slot = manager.get_active_slot()
    app = manager.active_app_root()
    if not (app / "backend" / "aiive" / "main.py").is_file():
        app = source_root()
        slot = ""
    env = {**os.environ, **slot_environment(app, slot, slots=manager.base_dir, runtime=manager.runtime_dir)}
    os.chdir(app / "backend")
    os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "aiive.main:app", *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
