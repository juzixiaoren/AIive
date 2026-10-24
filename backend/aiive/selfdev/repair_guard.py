"""修复运行的并发控制、候选版本隔离与中断恢复。

本模块只补三件缺失的事，不重复实现版本管理与晋升：

1. **并发控制**（``RepairLock``）：同一项目同一时间只允许一个修复流程改写
   候选槽位。锁文件由 ``O_CREAT|O_EXCL`` 原子创建，内容带 owner token；
   只有持有者能释放（token 不符时绝不删除别人的锁）；持有进程已退出时
   锁可被安全接管，持有进程仍存活时绝不抢占。
2. **候选隔离**（``CandidateSlotGuard``）：每轮修复先记录活跃槽位基线指纹，
   改写候选代码**之前**把候选槽位标记为"不可晋升"；失败后把候选槽位恢复
   为基线，恢复不了就保持"不可晋升"标记，避免失败代码被误晋升。
3. **中断恢复**（``RepairJournal`` + ``recover_interrupted``）：每个阶段落一份
   运行日志，下一次修复开始前扫描未完成的运行，按日志回滚候选槽位，并把
   该运行标记为 ``interrupted*``——绝不把不完整结果标记为成功。

指纹一律由 ``slot_manager.tree_fingerprint`` 计算（相对路径 + 文件内容），
``None` 表示"无法验证"，调用方必须按失败处理而不是按通过处理。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiive.supervisor.control import pid_alive
from aiive.supervisor.slot_manager import (
    SlotInfo,
    SlotManager,
    clear_candidate_state,
    read_candidate_state,
    sync_app_tree,
    tree_fingerprint,
    write_candidate_state,
)

logger = logging.getLogger(__name__)

# 仓库根：本文件位于 backend/aiive/selfdev/repair_guard.py
_REPO_ROOT = Path(__file__).resolve().parents[3]
# 修复运行状态目录（runtime/ 已在 .gitignore 中）
DEFAULT_STATE_DIR = _REPO_ROOT / "runtime" / "selfdev"

# 本进程当前真正持有的锁 token。
# 用于区分"同进程内另一个线程正在跑修复"（不可抢占）与
# "同进程上一轮遗留的孤儿锁"（可接管）。
_PROCESS_HELD_TOKENS: set[str] = set()

# 修复运行的终态：出现这些状态说明本次运行已结束，无需再恢复。
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "tests_passed",
    "promote_requested",
    "promoted",
    "apply_failed",
    "gate_failed",
    "promote_failed",
    "tests_failed",
    "blocked",
    "planner_failed",
    "repair_busy",
    "candidate_changed",
    "failed",
    "interrupted",
    "interrupted_recovered",
    "interrupted_quarantined",
    "interrupted_superseded",
    "interrupted_promotion_pending",
})


def _now() -> str:
    """返回 ISO-8601 UTC 时间字符串。"""
    return datetime.now(UTC).isoformat()


def project_lock_key(root: Path | str | None = None) -> str:
    """项目级锁名：同一套槽位目录下所有改写候选代码的入口共用一个锁。"""
    base = Path(root) if root is not None else _REPO_ROOT
    return hashlib.sha256(str(base).encode("utf-8")).hexdigest()[:12]


def default_state_dir(manager: SlotManager | None = None) -> Path:
    """修复运行状态目录：与槽位的 active_slot 标记同级的 runtime/selfdev。

    生产环境（默认 SlotManager）落在仓库 runtime/selfdev；测试或自定义
    槽位目录时随之落到该槽位的 runtime 目录，互不干扰。
    """
    if manager is None:
        return DEFAULT_STATE_DIR
    return manager.runtime_dir / "selfdev"


def default_repair_lock(manager: SlotManager | None = None) -> RepairLock:
    """默认的候选代码互斥锁：runtime/selfdev/locks/repair-<key>.lock。"""
    state_dir = default_state_dir(manager)
    key = project_lock_key(manager.base_dir if manager is not None else None)
    return RepairLock(state_dir / "locks" / f"repair-{key}.lock")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON（临时文件 + fsync + os.replace）。"""
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


# ── 并发控制 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class LockHandle:
    """一次成功获取的锁句柄；token 是释放时唯一的凭据。"""

    path: Path
    token: str
    run_id: str
    acquired_at: float

    def to_dict(self) -> dict[str, Any]:
        """序列化为可落库的字典（token 只用于本进程释放校验）。"""
        return {
            "path": str(self.path),
            "token": self.token,
            "run_id": self.run_id,
            "acquired_at": self.acquired_at,
        }


class RepairLock:
    """跨进程互斥锁：同一项目/槽位同一时间只允许一个修复流程。

    与 ``supervisor.control.FileLock`` 的差异（本场景必须的语义）：
    - 锁内容包含 owner token，``release`` 只在 token 匹配时删除文件，
      因此不会误释放已被其他运行实例接管的锁；
    - 陈旧判定只看持有进程是否存活：持有进程死亡 → 可安全接管；
      持有进程存活 → 绝不抢占（长跑测试/构建不会被误抢锁）。
    """

    def __init__(self, path: Path | str) -> None:
        self._path: Path = Path(path)
        self._handle: LockHandle | None = None

    @property
    def path(self) -> Path:
        """锁文件路径。"""
        return self._path

    @property
    def handle(self) -> LockHandle | None:
        """当前进程持有的句柄（未持有时为 None）。"""
        return self._handle

    def read_holder(self) -> dict[str, Any]:
        """读取当前锁文件内容；文件不存在或损坏时返回空字典。"""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def acquire(self, *, run_id: str, slot: str = "") -> LockHandle | None:
        """尝试获取锁；成功返回句柄，已被其他运行实例持有时返回 None。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        payload = {
            "run_id": run_id,
            "token": token,
            "pid": os.getpid(),
            "ts": time.time(),
            "slot": slot,
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        for _attempt in range(3):
            try:
                descriptor = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                holder = self.read_holder()
                if not self._is_stale(holder):
                    return None
                # 接管前复核：内容必须仍是刚才判定为陈旧的那份，
                # 否则说明有并发者刚重建了锁，必须重新判定。
                if self.read_holder().get("token") != holder.get("token"):
                    continue
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            _PROCESS_HELD_TOKENS.add(token)
            handle = LockHandle(
                path=self._path,
                token=token,
                run_id=run_id,
                acquired_at=float(payload["ts"]),
            )
            self._handle = handle
            return handle
        return None

    def release(self, handle: LockHandle | None = None) -> bool:
        """释放锁；只有 token 匹配（即本实例写入的锁）才会删除文件。"""
        target = handle or self._handle
        if target is None:
            return False
        _PROCESS_HELD_TOKENS.discard(target.token)
        holder = self.read_holder()
        if holder.get("token") != target.token:
            logger.warning(
                "锁已被其他运行实例接管，跳过释放: path=%s holder_run=%s",
                self._path,
                holder.get("run_id"),
            )
            return False
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        if self._handle is target:
            self._handle = None
        return True

    def _is_stale(self, holder: dict[str, Any]) -> bool:
        """判断锁是否可被接管（仅当持有进程已不存在或锁内容不可信）。"""
        pid = holder.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            # 锁文件损坏 / 无 pid：不可信，允许接管。
            return True
        if pid == os.getpid():
            # 同进程：只有本进程仍在持有的 token 才算活跃；
            # 其余情况是上一轮遗留的孤儿锁（可安全接管）。
            return holder.get("token") not in _PROCESS_HELD_TOKENS
        return not pid_alive(pid)


# ── 候选槽位隔离 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Baseline:
    """一次修复所依据的活跃版本基线。"""

    slot: str
    slot_root: Path
    app_root: Path
    version: str
    fingerprint: str
    captured_at: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为可落库的字典。"""
        return {
            "slot": self.slot,
            "slot_root": str(self.slot_root),
            "app_root": str(self.app_root),
            "version": self.version,
            "fingerprint": self.fingerprint,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Baseline | None:
        """从落库字典还原；结构不完整时返回 None（调用方按"无基线"处理）。"""
        if not isinstance(data, dict):
            return None
        slot = str(data.get("slot") or "")
        fingerprint = str(data.get("fingerprint") or "")
        slot_root = str(data.get("slot_root") or "")
        if slot not in {"A", "B"} or not fingerprint or not slot_root:
            return None
        root = Path(slot_root)
        return cls(
            slot=slot,
            slot_root=root,
            app_root=root / "app",
            version=str(data.get("version") or ""),
            fingerprint=fingerprint,
            captured_at=str(data.get("captured_at") or ""),
        )


class CandidateSlotGuard:
    """候选槽位的基线捕获、指纹核对、恢复与"不可晋升"标记。"""

    def __init__(self, manager: SlotManager | None = None) -> None:
        self._manager: SlotManager = manager or SlotManager()

    @property
    def manager(self) -> SlotManager:
        """本守卫使用的槽位管理器（供锁/日志定位同一套目录）。"""
        return self._manager

    # ── 槽位定位 ──

    def active_slot_name(self) -> str:
        """当前活跃槽位名称。"""
        return self._manager.get_active_slot()

    def _slot(self, name: str) -> SlotInfo | None:
        return next(
            (item for item in self._manager.list_slots() if item.name == name), None
        )

    def candidate_slot(self) -> SlotInfo | None:
        """当前候选槽位（非活跃槽位）。"""
        active = self._manager.get_active_slot()
        return self._slot("B" if active == "A" else "A")

    def candidate_app_root(self) -> Path | None:
        """候选槽位的 app 目录；槽位不存在时返回 None。"""
        slot = self.candidate_slot()
        return None if slot is None else slot.root / "app"

    # ── 基线 ──

    def capture_baseline(self) -> Baseline | None:
        """记录活跃槽位的版本与代码指纹；槽位未初始化/无 app 目录时返回 None。"""
        active = self._manager.get_active_slot()
        slot = self._slot(active)
        if slot is None:
            return None
        fingerprint = tree_fingerprint(slot.root / "app")
        if fingerprint is None:
            return None
        return Baseline(
            slot=slot.name,
            slot_root=slot.root,
            app_root=slot.root / "app",
            version=str(slot.manifest.get("version") or ""),
            fingerprint=fingerprint,
            captured_at=_now(),
        )

    def fingerprint_candidate(self) -> str | None:
        """候选槽位当前代码指纹；目录不存在返回 None。"""
        return tree_fingerprint(self.candidate_app_root())

    def baseline_drift(self, baseline: Baseline) -> str | None:
        """核对基线是否仍然有效；无效时返回原因。"""
        if self._manager.get_active_slot() != baseline.slot:
            return "baseline_slot_not_active"
        if tree_fingerprint(baseline.app_root) != baseline.fingerprint:
            return "baseline_fingerprint_changed"
        return None

    # ── 恢复 ──

    def restore(self, baseline: Baseline) -> dict[str, Any]:
        """把候选槽位恢复为已知基线，并复核恢复结果。

        只在"基线仍是活跃槽位且内容未变"时恢复——否则无法确定正确基线，
        保持候选槽位隔离，由调用方标记为不可晋升。
        """
        reason = self.baseline_drift(baseline)
        if reason is not None:
            return {"ok": False, "reason": reason}
        candidate = self.candidate_slot()
        if candidate is None:
            return {"ok": False, "reason": "candidate_slot_missing"}
        target_app = candidate.root / "app"
        try:
            sync_app_tree(baseline.app_root, target_app)
            source_manifest = baseline.slot_root / "version_manifest.json"
            if source_manifest.exists():
                target_manifest = candidate.root / "version_manifest.json"
                target_manifest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_manifest, target_manifest)
        except Exception as exc:  # 恢复失败必须显式隔离，不能静默
            logger.exception("候选槽位恢复失败")
            return {"ok": False, "reason": f"restore_failed: {exc}"}
        actual = tree_fingerprint(target_app)
        if actual != baseline.fingerprint:
            return {
                "ok": False,
                "reason": "restore_verification_failed",
                "expected": baseline.fingerprint,
                "actual": actual,
            }
        clear_candidate_state(candidate.root)
        return {
            "ok": True,
            "restored_to": baseline.slot,
            "fingerprint": actual,
        }

    # ── "不可晋升"标记 ──

    def read_state(self, slot_name: str) -> dict[str, Any]:
        """读取槽位的候选状态标记。"""
        slot = self._slot(slot_name)
        return {} if slot is None else read_candidate_state(slot.root)

    def mark_not_promotable(
        self, slot_name: str, *, reason: str, run_id: str = ""
    ) -> dict[str, Any]:
        """把槽位标记为不可晋升（fail-closed）。"""
        slot = self._slot(slot_name)
        if slot is None or not slot.root.is_dir():
            # 槽位尚未初始化：不创建目录、不写标记（避免污染未部署的仓库）。
            return {"ok": False, "reason": "slot_not_initialized"}
        payload = {
            "promotable": False,
            "reason": str(reason),
            "run_id": run_id,
            "fingerprint": "",
            "updated_at": _now(),
        }
        write_candidate_state(slot.root, payload)
        return {"ok": True, **payload}

    def mark_promotable(
        self,
        slot_name: str,
        *,
        run_id: str,
        fingerprint: str,
        gate_ok: bool,
    ) -> dict[str, Any]:
        """记录候选已通过测试/闸门，并绑定其代码指纹。"""
        slot = self._slot(slot_name)
        if slot is None or not slot.root.is_dir():
            return {"ok": False, "reason": "slot_not_initialized"}
        payload = {
            "promotable": True,
            "reason": "",
            "run_id": run_id,
            "fingerprint": str(fingerprint),
            "gate_ok": bool(gate_ok),
            "updated_at": _now(),
        }
        write_candidate_state(slot.root, payload)
        return {"ok": True, **payload}

    def clear_state(self, slot_name: str) -> None:
        """清除候选状态标记（候选已回到基线时使用）。"""
        slot = self._slot(slot_name)
        if slot is not None:
            clear_candidate_state(slot.root)


# ── 中断恢复 ──────────────────────────────────────────────────────


class RepairJournal:
    """修复运行日志：每个阶段一份快照，供进程中断后恢复。"""

    def __init__(self, runs_dir: Path | str) -> None:
        self._dir: Path = Path(runs_dir)

    def path_for(self, run_id: str) -> Path:
        """运行日志文件路径（run_id 做白名单过滤，防路径穿越）。"""
        safe = "".join(
            char for char in str(run_id) if char.isalnum() or char in {"-", "_"}
        )
        return self._dir / f"{safe or 'unknown'}.json"

    def write(self, record: dict[str, Any], *, phase: str) -> Path:
        """把当前运行状态写入日志（含阶段列表）。"""
        run_id = str(record.get("run_id") or "")
        phases = list(record.get("phases") or [])
        if phase not in phases:
            phases.append(phase)
        record["phases"] = phases
        lock = record.get("lock") or {}
        candidate = record.get("candidate") or {}
        payload = {
            "run_id": run_id,
            "trace_id": record.get("trace_id"),
            "goal": record.get("goal"),
            "pid": os.getpid(),
            "token": lock.get("token"),
            "status": record.get("status"),
            "ok": bool(record.get("ok")),
            "phase": phase,
            "phases": phases,
            "started_at": record.get("started_at"),
            "updated_at": _now(),
            "finished_at": record.get("finished_at"),
            "baseline": candidate.get("baseline"),
            "candidate_root": candidate.get("candidate_root"),
            "candidate_fingerprint": candidate.get("candidate_fingerprint"),
            "patch_fingerprint": record.get("final_patch_fingerprint"),
            "failure_reason": record.get("failure_reason"),
        }
        path = self.path_for(run_id)
        _atomic_write_json(path, payload)
        return path

    def read(self, run_id: str) -> dict[str, Any]:
        """读取一条运行日志；不存在或损坏时返回空字典。"""
        try:
            raw = json.loads(self.path_for(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def pending(self) -> list[dict[str, Any]]:
        """列出所有未到终态的运行日志（即可能需要恢复的运行）。"""
        if not self._dir.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "") in TERMINAL_STATUSES:
                continue
            entries.append(raw)
        return entries

    def prune(self, *, keep_days: float = 7.0, now: float | None = None) -> int:
        """清理过期的终态运行日志，避免目录无限增长（未完成的一律保留）。"""
        if not self._dir.is_dir():
            return 0
        deadline = (now if now is not None else time.time()) - keep_days * 86400
        removed = 0
        for path in self._dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            if str(raw.get("status") or "") not in TERMINAL_STATUSES:
                continue
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        failure_reason: str | None = None,
        phase: str = "recovery",
    ) -> dict[str, Any]:
        """把日志改为终态（恢复结果一律 ok=False，不冒充成功）。"""
        payload = self.read(run_id)
        if not payload:
            return {}
        payload["status"] = status
        payload["ok"] = False
        payload["failure_reason"] = failure_reason
        payload["phase"] = phase
        payload["updated_at"] = _now()
        payload["finished_at"] = payload.get("finished_at") or _now()
        phases = list(payload.get("phases") or [])
        if phase not in phases:
            phases.append(phase)
        payload["phases"] = phases
        _atomic_write_json(self.path_for(run_id), payload)
        return payload


def recover_interrupted(
    journal: RepairJournal,
    guard: CandidateSlotGuard,
    *,
    exclude_run_id: str = "",
) -> list[dict[str, Any]]:
    """扫描未完成的修复运行，恢复候选槽位或显式隔离。

    必须在持有 ``RepairLock`` 后调用：此时本项目的其他修复流程要么已结束，
    要么已死亡。持有进程仍然存活的运行一律跳过（不抢活着的运行）。
    """
    journal.prune()
    results: list[dict[str, Any]] = []
    for entry in journal.pending():
        run_id = str(entry.get("run_id") or "")
        if not run_id or run_id == exclude_run_id:
            continue
        pid = entry.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and pid_alive(pid):
            results.append({
                "run_id": run_id,
                "action": "skipped_live_owner",
                "pid": pid,
                "phase": entry.get("phase"),
            })
            continue

        baseline = Baseline.from_dict(entry.get("baseline"))
        candidate = guard.candidate_slot()
        candidate_name = candidate.name if candidate is not None else ""
        action: dict[str, Any] = {
            "run_id": run_id,
            "pid": pid,
            "phase": entry.get("phase"),
            "status_before": entry.get("status"),
        }

        if baseline is None:
            # 没有可用基线：无法判断候选内容是否正确，只能隔离。
            marked = False
            if candidate_name:
                marked = bool(guard.mark_not_promotable(
                    candidate_name,
                    reason="interrupted_without_baseline",
                    run_id=run_id,
                ).get("ok"))
            journal.finish(
                run_id,
                status="interrupted",
                failure_reason="interrupted_without_baseline",
            )
            action["quarantined"] = marked
            action["action"] = "quarantined" if marked else "interrupted"
            results.append(action)
            continue

        if guard.active_slot_name() != baseline.slot:
            # 基线槽位已不再活跃：该运行要么已晋升，要么被人为切换，
            # 此时回滚候选槽位只会破坏现状，仅记录为"被取代"。
            journal.finish(
                run_id,
                status="interrupted_superseded",
                failure_reason="baseline_slot_not_active",
            )
            action["action"] = "superseded"
            results.append(action)
            continue

        if str(entry.get("phase") or "") == "promoting":
            # 中断发生在提交晋升请求时：候选槽位里是通过闸门的代码，
            # 且 Supervisor 可能已受理请求。此时回滚会破坏待执行的晋升，
            # 因此保持候选原样，只把运行标记为"晋升结果未知"。
            journal.finish(
                run_id,
                status="interrupted_promotion_pending",
                failure_reason="promotion_outcome_unknown",
            )
            action["action"] = "promotion_pending"
            results.append(action)
            continue

        current = guard.fingerprint_candidate()
        if current is not None and current == baseline.fingerprint:
            # 候选与基线一致（中断发生在写入之前或已完成回滚）：无需复制。
            guard.clear_state(candidate_name)
            journal.finish(
                run_id,
                status="interrupted_recovered",
                failure_reason="candidate_already_matches_baseline",
            )
            action["action"] = "noop_already_baseline"
            results.append(action)
            continue

        restored = guard.restore(baseline)
        action["restore"] = restored
        if restored.get("ok"):
            journal.finish(
                run_id,
                status="interrupted_recovered",
                failure_reason="interrupted_restored_to_baseline",
            )
            action["action"] = "restored"
        else:
            if candidate_name:
                guard.mark_not_promotable(
                    candidate_name,
                    reason="recovery_failed: " + str(restored.get("reason") or "unknown"),
                    run_id=run_id,
                )
                action["quarantined"] = True
            journal.finish(
                run_id,
                status="interrupted_quarantined",
                failure_reason="recovery_failed: " + str(restored.get("reason") or "unknown"),
            )
            action["action"] = "quarantined"
        results.append(action)
    return results


# ── 手动改写入口的并发保护 ────────────────────────────────────────


def apply_candidate_patch(
    executor: Any,
    operations: list[dict[str, Any]],
    *,
    guard: CandidateSlotGuard | None = None,
    lock: RepairLock | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    """在并发锁保护下改写候选槽位，并返回绑定好基线与指纹的结果。

    与 SelfDevRepairLoop 共用同一把锁与同一套候选状态标记：手动 apply
    （REST /apply-inactive 与 apply_patch_to_inactive_slot 工具）与自主修复
    不可能同时改写候选代码；改写期间槽位一律标记为不可晋升。
    """
    guard = guard or CandidateSlotGuard(
        manager=getattr(executor, "_manager", None)
    )
    lock = lock or default_repair_lock(guard.manager)
    run_id = run_id or uuid4().hex
    handle = lock.acquire(run_id=run_id)
    if handle is None:
        return {
            "ok": False,
            "error": "concurrent_repair_in_progress",
            "holder": lock.read_holder(),
            "operations_applied": [],
            "operations_failed": [],
            "run_id": run_id,
            "lock": {"acquired": False},
        }
    baseline: Baseline | None = None
    slot: SlotInfo | None = None
    result: dict[str, Any] = {
        "ok": False,
        "error": "patch executor did not run",
        "operations_applied": [],
        "operations_failed": [],
    }
    try:
        baseline = guard.capture_baseline()
        slot = guard.candidate_slot()
        if baseline is not None and slot is not None:
            guard.mark_not_promotable(
                slot.name, reason="repair_in_progress", run_id=run_id
            )
        applied = executor.apply_to_inactive(operations)
        result = (
            applied
            if isinstance(applied, dict)
            else {
                "ok": False,
                "error": "patch executor returned invalid result",
                "operations_applied": [],
                "operations_failed": [],
            }
        )
    except Exception as exc:
        logger.exception("候选补丁应用异常")
        result = {
            "ok": False,
            "error": f"patch executor crashed: {exc}",
            "operations_applied": [],
            "operations_failed": [],
        }
    finally:
        released = lock.release(handle)
    candidate_root = str(result.get("candidate_root") or "")
    candidate_slot = str(result.get("inactive_slot") or (slot.name if slot else ""))
    return {
        **result,
        "run_id": run_id,
        "lock": {"acquired": True, "released": released},
        "candidate": {
            "baseline": baseline.to_dict() if baseline else None,
            "candidate_slot": candidate_slot,
            "candidate_root": candidate_root,
            "candidate_fingerprint": guard.fingerprint_candidate(),
        },
    }


def finalize_candidate_patch(
    result: dict[str, Any],
    *,
    tests_ok: bool,
    guard: CandidateSlotGuard,
    reason: str = "",
) -> dict[str, Any]:
    """按定向测试结果给候选槽位定状态：通过则登记指纹，否则保持不可晋升。

    guard 必须与产生 result 的执行器使用同一个槽位管理器，否则标记会写到
    另一套槽位目录上；因此这里要求显式传参，不提供默认构造。
    """
    candidate = result.get("candidate") or {}
    run_id = str(result.get("run_id") or "")
    slot_name = str(candidate.get("candidate_slot") or "")
    fingerprint = str(candidate.get("candidate_fingerprint") or "")
    if not slot_name:
        slot = guard.candidate_slot()
        slot_name = slot.name if slot else ""
    if not slot_name:
        return {"ok": False, "promotable": False, "skipped": "no_candidate_slot"}
    if tests_ok:
        if not fingerprint:
            # 无法指纹化（槽位未初始化/目录缺失）：不冒充"已验证"。
            return {
                "ok": False,
                "promotable": False,
                "slot": slot_name,
                "skipped": "fingerprint_unavailable",
            }
        guard.mark_promotable(
            slot_name, run_id=run_id, fingerprint=fingerprint, gate_ok=False,
        )
        return {
            "ok": True,
            "promotable": True,
            "slot": slot_name,
            "fingerprint": fingerprint,
        }
    failure = reason or "targeted_tests_failed"
    guard.mark_not_promotable(slot_name, reason=failure, run_id=run_id)
    return {"ok": False, "promotable": False, "slot": slot_name, "reason": failure}
