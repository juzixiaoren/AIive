import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


SLOTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent / "slots"
ACTIVE_SLOT_FILE = (
    Path(__file__).resolve().parent.parent.parent.parent.parent / "runtime" / "active_slot"
)


@dataclass
class SlotInfo:
    name: str  # "A" | "B"
    root: Path
    active: bool
    manifest: dict = field(default_factory=dict)
    manifest_checksum: str = ""


class SlotManager:
    def __init__(self, base_dir: Path | None = None):
        self._base_dir = base_dir or SLOTS_ROOT
        self._active_file = base_dir.parent / "runtime" / "active_slot" if base_dir else ACTIVE_SLOT_FILE

    def get_active_slot(self) -> str:
        if self._active_file.exists():
            return self._active_file.read_text().strip()
        return "A"

    def set_active_slot(self, name: str) -> None:
        self._active_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_file.write_text(name)

    def list_slots(self) -> list[SlotInfo]:
        active = self.get_active_slot()
        result: list[SlotInfo] = []
        for name in ("A", "B"):
            root = self._base_dir / name
            manifest = self._read_manifest(root)
            checksum = _compute_manifest_checksum(manifest) if manifest else ""
            result.append(SlotInfo(
                name=name,
                root=root,
                active=(name == active),
                manifest=manifest,
                manifest_checksum=checksum,
            ))
        return result

    def init_slots(self) -> None:
        for name in ("A", "B"):
            slot_dir = self._base_dir / name / "app"
            slot_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "slot": name,
                "version": "0.1.0",
                "includes": ["backend/", "frontend/", "tests/", "scripts/"],
                "excludes": ["data/", "postgres/", "qdrant/", "object_store/", "logs/", ".env"],
                "config_templates": [".env.example"],
                "dependency_files": ["pyproject.toml", "frontend/package.json"],
            }
            manifest_path = slot_dir.parent / "version_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2))

        self.set_active_slot("A")

    def _read_manifest(self, slot_root: Path) -> dict:
        manifest_path = slot_root / "version_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}


def _compute_manifest_checksum(manifest: dict) -> str:
    raw = json.dumps(manifest, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def create_version_manifest(slot: str, version: str = "0.1.0") -> dict:
    return {
        "slot": slot,
        "version": version,
        "includes": ["backend/", "frontend/", "tests/", "scripts/"],
        "excludes": [
            "data/", "postgres/", "qdrant/", "object_store/",
            "logs/", ".env", "slots/", "runtime/",
        ],
        "config_templates": [".env.example"],
        "dependency_files": ["pyproject.toml", "frontend/package.json"],
    }
