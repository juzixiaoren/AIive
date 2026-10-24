"""
槽位管理模块。

实现 A/B 双槽位（slot）管理系统，支持蓝绿部署模式。
每个槽位包含独立的代码副本和版本 manifest，通过活跃槽位文件进行切换。
提供槽位初始化、状态查询、活跃槽位读写和 manifest 校验功能。
"""

import hashlib
import json
import logging
import os
import shutil
from uuid import uuid4

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

    @property
    def base_dir(self) -> Path:
        """槽位根目录（A/B 所在目录）。"""
        return self._base_dir

    @property
    def runtime_dir(self) -> Path:
        """运行时目录：与 active_slot 标记文件同级。"""
        return self._active_file.parent

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
        if name not in {"A", "B"}:
            raise ValueError("active slot must be A or B")
        self._active_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._active_file.parent / f".{self._active_file.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(name)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._active_file)
            # Best-effort directory fsync makes the rename durable across a crash.
            try:
                directory_fd = os.open(self._active_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                logger.debug("active slot directory fsync unavailable", exc_info=True)
        finally:
            temporary.unlink(missing_ok=True)

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
            manifest = create_version_manifest(name)
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


# 候选槽位状态文件：与 app/ 同级，记录该槽位当前是否允许晋升。
CANDIDATE_STATE_FILE = "candidate_state.json"

# 指纹计算时忽略的目录：构建产物与缓存不属于候选代码语义。
_FINGERPRINT_IGNORED_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "dist", "build", ".next", "coverage",
})
_FINGERPRINT_IGNORED_SUFFIXES: frozenset[str] = frozenset({".pyc", ".pyo"})


def tree_fingerprint(root: Path | str | None) -> str | None:
    """计算目录树的确定性指纹（相对路径 + 文件内容 SHA-256）。

    参数:
        root: 目录路径；None 或目录不存在时返回 None。

    返回:
        十六进制指纹字符串；**None 表示"无法验证"**，调用方必须按失败处理，
        不能当成"验证通过"。
    """
    if root is None:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    entries: list[str] = []
    for path in sorted(base.rglob("*")):
        try:
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
        except OSError:
            continue
        if any(part in _FINGERPRINT_IGNORED_DIRS for part in relative.split("/")[:-1]):
            continue
        if path.suffix in _FINGERPRINT_IGNORED_SUFFIXES:
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            continue
        entries.append(f"{relative}:{digest.hexdigest()}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def sync_app_tree(source_app: Path, target_app: Path) -> None:
    """把 source_app 完整同步为 target_app（先删旧树，再整树复制）。"""
    if source_app.exists():
        if target_app.exists():
            shutil.rmtree(target_app)
        shutil.copytree(source_app, target_app)
    target_app.mkdir(parents=True, exist_ok=True)


def candidate_state_path(slot_root: Path | str) -> Path:
    """槽位候选状态文件路径。"""
    return Path(slot_root) / CANDIDATE_STATE_FILE


def read_candidate_state(slot_root: Path | str) -> dict[str, Any]:
    """读取槽位候选状态；文件不存在或损坏时返回空字典。"""
    try:
        raw = json.loads(candidate_state_path(slot_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_candidate_state(slot_root: Path | str, payload: dict[str, Any]) -> None:
    """原子写入槽位候选状态。"""
    path = candidate_state_path(slot_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_candidate_state(slot_root: Path | str) -> None:
    """删除槽位候选状态文件。"""
    candidate_state_path(slot_root).unlink(missing_ok=True)


def candidate_promotion_block(slot_root: Path | str) -> str | None:
    """晋升前的候选状态核对（fail-closed）：返回阻断原因，None 表示放行。

    - 状态标记 promotable=false：槽位里是失败/未验证的代码，禁止晋升；
    - 登记的代码指纹与当前 app/ 不一致：验证之后代码被改动，禁止晋升。
    没有状态文件时返回 None，保持未接入修复流程的槽位原有行为。
    """
    state = read_candidate_state(slot_root)
    if not state:
        return None
    if state.get("promotable") is False:
        return "candidate_not_promotable: " + str(state.get("reason") or "unknown")
    expected = str(state.get("fingerprint") or "")
    if not expected:
        return None
    if tree_fingerprint(Path(slot_root) / "app") != expected:
        return (
            "candidate_fingerprint_mismatch: candidate code changed after verification"
        )
    return None


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
