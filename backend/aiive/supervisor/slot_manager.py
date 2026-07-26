"""
槽位管理模块。

实现 A/B 双槽位（slot）管理系统，支持蓝绿部署模式。
每个槽位包含独立的代码副本和版本 manifest，通过活跃槽位文件进行切换。
提供槽位初始化、状态查询、活跃槽位读写和 manifest 校验功能。
"""

import hashlib
import json
import logging

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 仓库根：本文件位于 backend/aiive/supervisor/slot_manager.py
# parents[3] = supervisor -> aiive -> backend -> 仓库根（与 manifest excludes
# 中 slots/、runtime/ 的相对目录约定一致，且已被 .gitignore 覆盖）
_REPO_ROOT = Path(__file__).resolve().parents[3]
# 槽位根目录
SLOTS_ROOT = _REPO_ROOT / "slots"
# 活跃槽位标记文件
ACTIVE_SLOT_FILE = _REPO_ROOT / "runtime" / "active_slot"


@dataclass
class SlotInfo:
    """槽位信息数据类。"""
    name: str  # 槽位名称："A" 或 "B"
    root: Path  # 槽位根目录
    active: bool  # 是否为当前活跃槽位
    manifest: dict[str, Any] = field(default_factory=dict)  # 版本 manifest 内容
    manifest_checksum: str = ""  # manifest 的 SHA-256 校验和（前16位）


class SlotManager:
    """A/B 槽位管理器，负责槽位的创建、查询和切换。"""

    def __init__(self, base_dir: Path | None = None):
        """
        初始化槽位管理器。

        参数:
            base_dir: 槽位基础目录，默认使用 SLOTS_ROOT。
        """
        self._base_dir: Path = base_dir or SLOTS_ROOT
        self._active_file: Path = base_dir.parent / "runtime" / "active_slot" if base_dir else ACTIVE_SLOT_FILE

    def get_active_slot(self) -> str:
        """
        获取当前活跃槽位名称。

        返回:
            "A" 或 "B"。默认返回 "A"。
        """
        if self._active_file.exists():
            return self._active_file.read_text().strip()
        return "A"

    def set_active_slot(self, name: str) -> None:
        """
        设置当前活跃槽位。

        参数:
            name: 槽位名称（"A" 或 "B"）。
        """
        self._active_file.parent.mkdir(parents=True, exist_ok=True)
        self._active_file.write_text(name)

    def list_slots(self) -> list[SlotInfo]:
        """
        列出所有槽位信息。

        返回:
            SlotInfo 列表，包含 A 和 B 两个槽位的详情。
        """
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
        """
        初始化 A/B 槽位结构。

        创建槽位目录、写入默认 version_manifest.json，并设置 A 为活跃槽位。
        """
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

    def _read_manifest(self, slot_root: Path) -> dict[str, Any]:
        """
        读取槽位的 version_manifest.json。

        参数:
            slot_root: 槽位根目录。

        返回:
            manifest 字典，文件不存在时返回空字典。
        """
        manifest_path = slot_root / "version_manifest.json"
        if manifest_path.exists():
            return json.loads(manifest_path.read_text())
        return {}


def _compute_manifest_checksum(manifest: dict[str, Any]) -> str:
    """
    计算 manifest 的 SHA-256 校验和。

    参数:
        manifest: manifest 字典。

    返回:
        校验和的前 16 位十六进制字符串。
    """
    raw = json.dumps(manifest, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def create_version_manifest(slot: str, version: str = "0.1.0") -> dict[str, Any]:
    """
    创建新的版本 manifest 字典。

    参数:
        slot: 槽位名称。
        version: 版本号，默认 "0.1.0"。

    返回:
        包含 slot、version、includes、excludes 等字段的 manifest 字典。
    """
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
