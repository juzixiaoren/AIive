"""能力生命周期服务：验证、启用、停用、升级、回滚、卸载与一致性核查。

设计要点（与既有实现的边界）：

- **不重复实现安装**：安装仍由 `aiive.mcp.installer` 负责，本模块只在升级时
  调用 `install_to_dir` 装到版本化目录，切换前完成验证。
- **不重复实现注册**：注册/反注册统一走 `aiive.mcp.bootstrap`，ToolRegistry 是
  发现索引的唯一事实来源；本模块只负责在正确时机调用并记录结果。
- **不绕过执行安全边界**：启用只决定"能否被发现"，执行仍由 CapabilityBroker +
  TaskPolicyEngine + 审批 + 资源锁 + descriptor_hash 裁决。
- **四个事实分开记录**：安装成功（installed_at）、验证通过（verified_at）、
  注册成功（registered_at）、正式启用（enabled_at）各自独立，任一环节失败不会被
  前一环节的成功掩盖。

并发与幂等：

- 同一 capability_id 的写操作在进程内串行（RLock），数据库侧再叠加
  `SELECT ... FOR UPDATE`（PostgreSQL 生效，SQLite 为无操作）。
- 重复请求按状态返回 `already_*` 且 `changed=False`，不重复写库、不重复注册。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aiive.capability.states import (
    InvalidCapabilityTransition,
    allowed_transitions,
    ensure_transition,
)
from aiive.db.models import (
    AgentAction,
    AgentTask,
    Capability,
    CapabilityVersion,
    MCPInstallRecord,
)
from aiive.mcp import installer as installer_mod
from aiive.mcp.bootstrap import (
    activate_capability_tools,
    registered_tool_ids,
    unregister_capability_tools,
)
from aiive.mcp.installer import SANDBOX_ROOT, run_smoke, sanitize_dir_name

logger = logging.getLogger(__name__)

# 视为"仍在占用能力"的 Action 状态：这些状态下删底层资源会打断执行。
IN_FLIGHT_ACTION_STATES: frozenset[str] = frozenset({
    "planned", "validated", "awaiting_approval", "ready", "dispatched", "running",
})
# 未终结的 Task 状态：这些任务可能随时派发新 Action，卸载前需要检查引用。
ACTIVE_TASK_STATES: frozenset[str] = frozenset({
    "queued", "dispatching", "running", "blocked_approval", "blocked_user",
    "blocked_node", "reconciling", "verifying",
})
# 生命周期历史保留条数（防止 definition JSON 无限增长）。
_HISTORY_LIMIT = 20
# 升级时新版本安装目录后缀分隔符：<name>@<version>，与旧目录并存以便回滚。
_VERSION_DIR_SEPARATOR = "@"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _capability_lock(capability_id: str) -> threading.RLock:
    """取（或创建）单个能力的进程内可重入锁。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(capability_id)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[capability_id] = lock
        return lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _lifecycle_block(definition: dict[str, Any]) -> dict[str, Any]:
    """取（必要时创建）definition 中的 lifecycle 元数据块。"""
    block = definition.get("lifecycle")
    if not isinstance(block, dict):
        block = {}
        definition["lifecycle"] = block
    return block


def _definition_fingerprint(definition: dict[str, Any]) -> str:
    """能力"定义本体"的稳定指纹（不含安装路径、工具缓存与生命周期时间戳）。

    用于判断"验证之后定义是否被改过"：改过就必须重新验证，不能沿用旧结论。
    """
    basis = {
        key: definition.get(key)
        for key in (
            "name", "source", "description", "risk_notes", "trust_level",
            "package_ref", "version", "transport", "declared_tools",
        )
        if key in definition
    }
    raw = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _result(
    *,
    capability_id: str,
    action: str,
    ok: bool,
    state: str = "",
    error: str = "",
    error_type: str = "",
    changed: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一的生命周期操作返回值。"""
    return {
        "ok": ok,
        "capability_id": capability_id,
        "action": action,
        "state": state,
        "error": error,
        "error_type": error_type,
        "changed": changed,
        "details": details or {},
    }


class CapabilityLifecycleService:
    """能力生命周期的唯一写入口。

    所有方法都在事务内 flush，并在成功路径 commit；失败路径不提交半成品状态。
    """

    def __init__(
        self,
        db: Session,
        *,
        registry: Any | None = None,
        client: Any | None = None,
        sandbox_root: Path | None = None,
    ) -> None:
        self.db: Session = db
        self._registry_override: Any | None = registry
        self._client_override: Any | None = client
        self.sandbox_root: Path = sandbox_root or SANDBOX_ROOT

    # ------------------------------------------------------------------
    # 依赖解析
    # ------------------------------------------------------------------

    def _registry(self) -> Any:
        if self._registry_override is not None:
            return self._registry_override
        from aiive.tools.registry import get_tool_registry

        return get_tool_registry()

    def _client(self) -> Any:
        if self._client_override is not None:
            return self._client_override
        from aiive.mcp.runtime_client import get_runtime_client

        return get_runtime_client()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _find(self, capability_id: str) -> Capability | None:
        """按 id 查能力：先精确匹配，再补 mcp: 前缀（API 允许省略前缀）。"""
        raw = (capability_id or "").strip()
        if not raw:
            return None
        row = (
            self.db.query(Capability)
            .filter(Capability.capability_id == raw)
            .first()
        )
        if row is not None:
            return row
        if not raw.startswith("mcp:"):
            return (
                self.db.query(Capability)
                .filter(Capability.capability_id == f"mcp:{raw}")
                .first()
            )
        return None

    def _find_locked(self, capability_id: str) -> Capability | None:
        """加行锁读取（PostgreSQL 下串行化并发写；SQLite 退化为普通读）。"""
        raw = (capability_id or "").strip()
        candidates = [raw] if raw.startswith("mcp:") else [raw, f"mcp:{raw}"]
        for candidate in candidates:
            if not candidate:
                continue
            row = (
                self.db.query(Capability)
                .filter(Capability.capability_id == candidate)
                .with_for_update()
                .first()
            )
            if row is not None:
                return row
        return None

    def _installs(self, cap: Capability) -> list[MCPInstallRecord]:
        return (
            self.db.query(MCPInstallRecord)
            .filter(MCPInstallRecord.capability_id == cap.id)
            .order_by(MCPInstallRecord.created_at.desc())
            .all()
        )

    def _versions(self, cap: Capability) -> list[CapabilityVersion]:
        return (
            self.db.query(CapabilityVersion)
            .filter(CapabilityVersion.capability_id == cap.id)
            .order_by(CapabilityVersion.created_at.desc())
            .all()
        )

    def _in_flight_actions(self, cap: Capability) -> list[dict[str, str]]:
        """仍在引用该能力工具的 Action（用于停用提示与卸载拒绝）。"""
        tool_ids = registered_tool_ids(cap)
        if not tool_ids:
            return []
        rows = (
            self.db.query(AgentAction)
            .filter(
                AgentAction.capability_id.in_(tool_ids),
                AgentAction.status.in_(sorted(IN_FLIGHT_ACTION_STATES)),
            )
            .limit(50)
            .all()
        )
        return [
            {"action_id": row.id, "task_id": row.task_id, "status": row.status}
            for row in rows
        ]

    def _referencing_tasks(self, cap: Capability) -> list[dict[str, str]]:
        """未终结任务里显式声明使用该能力的记录（卸载前依赖检查）。"""
        tool_ids = set(registered_tool_ids(cap))
        if not tool_ids:
            return []
        rows = (
            self.db.query(AgentTask)
            .filter(AgentTask.status.in_(sorted(ACTIVE_TASK_STATES)))
            .limit(200)
            .all()
        )
        referencing: list[dict[str, str]] = []
        for task in rows:
            brief: dict[str, Any] = task.task_brief or {}
            allowed = brief.get("allowed_capabilities")
            if not isinstance(allowed, list):
                continue
            if tool_ids & {str(item) for item in allowed}:
                referencing.append({"task_id": task.id, "status": task.status})
        return referencing

    def status(self, capability_id: str) -> dict[str, Any]:
        """返回能力的生命周期视图（四个事实分列，不做布尔合并）。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="status", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        definition = copy.deepcopy(cap.definition or {})
        lifecycle = definition.get("lifecycle")
        meta = lifecycle if isinstance(lifecycle, dict) else {}
        installs = self._installs(cap)
        launch = definition.get("launch")
        sandbox_path = (
            str(launch.get("sandbox_path", "")) if isinstance(launch, dict) else ""
        )
        artifact_ok = bool(sandbox_path) and Path(sandbox_path).exists()
        tool_ids = registered_tool_ids(cap)
        registry = self._registry()
        missing_tools = [
            tool_id for tool_id in tool_ids if registry.get(tool_id) is None
        ]
        try:
            transitions = sorted(allowed_transitions(cap.state))
        except InvalidCapabilityTransition:
            transitions = []
        return _result(
            capability_id=cap.capability_id,
            action="status",
            ok=True,
            state=cap.state,
            details={
                "name": cap.name,
                "version": str(definition.get("version", "")),
                "descriptor_hash": cap.descriptor_hash or "",
                "installed": bool(installs) or artifact_ok,
                "verified": bool(meta.get("verified_at")),
                "registered": bool(tool_ids) and not missing_tools,
                "enabled": cap.state == "active",
                "artifact_present": artifact_ok,
                "sandbox_path": sandbox_path,
                "tool_count": len(tool_ids),
                "registered_tools": [t for t in tool_ids if t not in missing_tools],
                "missing_tools": missing_tools,
                "in_flight": self._in_flight_actions(cap),
                "allowed_transitions": transitions,
                "lifecycle": meta,
            },
        )

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------

    def probe(self, cap: Capability) -> dict[str, Any]:
        """真实启动 MCP server 做 tools/list，并把结果落成 verified/needs_review。

        不伪造成功：任一环节失败都如实返回 ok=False，并把错误写进冒烟记录。
        """
        definition = copy.deepcopy(cap.definition or {})
        launch = definition.get("launch")
        if not isinstance(launch, dict):
            smoke: dict[str, Any] = {
                "ok": False, "mode": "tools_list", "error": "missing launch config",
            }
            run_smoke(self.db, cap.capability_id, smoke, auto_activate=False)
            return smoke

        from aiive.mcp.runtime_client import build_launch_spec

        try:
            spec = build_launch_spec(launch)
            client = self._client()
            client.configure(cap.capability_id, spec)
            tools = client.list_tools(cap.capability_id)
        except Exception as error:  # 启动/握手/tools/list 任一失败
            logger.warning(
                "[TRACE:capability] VERIFY_FAILED capability=%s error=%s",
                cap.capability_id, error,
            )
            smoke = {"ok": False, "mode": "tools_list", "error": str(error)}
            run_smoke(self.db, cap.capability_id, smoke, auto_activate=False)
            return smoke

        real_names = [str(tool.get("name", "")) for tool in tools]
        installs = self._installs(cap)
        declared = set(installs[0].declared_tools or []) if installs else set()
        smoke = {
            "ok": len(real_names) > 0,
            "mode": "tools_list",
            "real_tools": real_names,
            "declared_missing": sorted(declared - set(real_names)),
            "undeclared_extra": sorted(set(real_names) - declared),
            "error": None if real_names else "server started but exposes no tools",
        }
        if smoke["ok"]:
            definition["tools"] = tools
            cap.definition = definition
        run_smoke(self.db, cap.capability_id, smoke, auto_activate=False)
        return {**smoke, "tools": tools}

    def verify(self, capability_id: str) -> dict[str, Any]:
        """重新验证：状态推进到 verified（通过）或 needs_review（失败）。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="verify", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="verify", ok=False,
                    error="capability_not_found", error_type="not_found",
                )
            if cap.state == "active":
                return _result(
                    capability_id=cap.capability_id, action="verify", ok=False,
                    state=cap.state, error="capability_active",
                    error_type="invalid_state",
                    details={"hint": "已启用能力需先停用再重新验证"},
                )
            if not isinstance((cap.definition or {}).get("launch"), dict):
                return _result(
                    capability_id=cap.capability_id, action="verify", ok=False,
                    state=cap.state, error="capability_not_installed",
                    error_type="not_installed",
                )
            before = cap.state
            smoke = self.probe(cap)
            definition = copy.deepcopy(cap.definition or {})
            meta = _lifecycle_block(definition)
            meta["last_verify_ok"] = bool(smoke.get("ok"))
            if smoke.get("ok"):
                meta["verified_at"] = _iso(_utcnow())
                meta["verified_fingerprint"] = _definition_fingerprint(definition)
            else:
                meta["last_error"] = str(smoke.get("error") or "verification failed")
            self._append_history(
                definition, "verify", before, cap.state, bool(smoke.get("ok")),
                str(smoke.get("error") or ""),
            )
            cap.definition = definition
            self.db.flush()
            self.db.commit()
            return _result(
                capability_id=cap.capability_id,
                action="verify",
                ok=bool(smoke.get("ok")),
                state=cap.state,
                changed=before != cap.state,
                error="" if smoke.get("ok") else str(smoke.get("error") or ""),
                error_type="" if smoke.get("ok") else "verification_failed",
                details={
                    "smoke": {k: v for k, v in smoke.items() if k != "tools"},
                    "tool_count": len(smoke.get("real_tools") or []),
                },
            )

    # ------------------------------------------------------------------
    # 启用 / 停用
    # ------------------------------------------------------------------

    def enable(self, capability_id: str) -> dict[str, Any]:
        """启用能力：校验定义与产物 → 注册工具 → 状态置 active。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="enable", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="enable", ok=False,
                    error="capability_not_found", error_type="not_found",
                )
            definition = copy.deepcopy(cap.definition or {})
            meta = _lifecycle_block(definition)
            launch = definition.get("launch")
            if not isinstance(launch, dict):
                return _result(
                    capability_id=cap.capability_id, action="enable", ok=False,
                    state=cap.state, error="capability_not_installed",
                    error_type="not_installed",
                )
            sandbox_path = launch.get("sandbox_path")
            if isinstance(sandbox_path, str) and sandbox_path:
                if not Path(sandbox_path).exists():
                    self._mark_needs_review(cap, definition, "sandbox artifacts missing")
                    return _result(
                        capability_id=cap.capability_id, action="enable", ok=False,
                        state=cap.state, error="sandbox artifacts missing",
                        error_type="artifact_missing",
                    )
            verified_fingerprint = meta.get("verified_fingerprint")
            if (
                isinstance(verified_fingerprint, str)
                and verified_fingerprint
                and verified_fingerprint != _definition_fingerprint(definition)
            ):
                return _result(
                    capability_id=cap.capability_id, action="enable", ok=False,
                    state=cap.state, error="definition_changed_since_verification",
                    error_type="definition_changed",
                    details={"hint": "定义已变化，请先重新验证"},
                )
            if cap.state not in ("verified", "disabled", "active"):
                # 未验证的能力不得启用：先 verify 再 enable，两步各自留痕。
                return _result(
                    capability_id=cap.capability_id, action="enable", ok=False,
                    state=cap.state,
                    error=f"capability must be verified before enable (state={cap.state})",
                    error_type="invalid_state",
                    details={"hint": "先调用 verify 完成真实验证"},
                )

            was_active = cap.state == "active"
            registry = self._registry()
            activation = activate_capability_tools(registry, cap)
            if not activation.get("ok"):
                meta["last_error"] = str(activation.get("error") or "registration failed")
                cap.definition = definition
                self.db.flush()
                self.db.commit()
                return _result(
                    capability_id=cap.capability_id, action="enable", ok=False,
                    state=cap.state, error=str(activation.get("error") or ""),
                    error_type="registration_failed",
                )

            registered = [str(item) for item in activation.get("registered", [])]
            now = _iso(_utcnow())
            meta["registered_at"] = now
            meta["registered_tools"] = registered
            meta["last_error"] = ""
            if not was_active:
                ensure_transition(cap.state, "active")
                meta["enabled_at"] = now
                cap.state = "active"
            else:
                meta.setdefault("enabled_at", now)
            self._append_history(
                definition, "enable", cap.state if was_active else "verified",
                cap.state, True, "",
            )
            cap.definition = definition
            self.db.flush()
            self.db.commit()
            return _result(
                capability_id=cap.capability_id, action="enable", ok=True,
                state=cap.state, changed=not was_active,
                details={
                    "registered": registered,
                    "already_enabled": was_active,
                },
            )

    def disable(self, capability_id: str) -> dict[str, Any]:
        """停用能力：从发现索引移除，保留安装产物与用户配置。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="disable", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="disable", ok=False,
                    error="capability_not_found", error_type="not_found",
                )
            definition = copy.deepcopy(cap.definition or {})
            meta = _lifecycle_block(definition)
            if cap.state == "disabled":
                return _result(
                    capability_id=cap.capability_id, action="disable", ok=True,
                    state=cap.state, details={"already_disabled": True},
                )
            if cap.state != "active":
                return _result(
                    capability_id=cap.capability_id, action="disable", ok=False,
                    state=cap.state, error=f"cannot disable from state {cap.state}",
                    error_type="invalid_state",
                )
            ensure_transition(cap.state, "disabled")
            removed = unregister_capability_tools(self._registry(), cap)
            try:
                self._client().close(cap.capability_id)
            except Exception as error:  # 会话已断开/从未建立都不影响停用结论
                logger.warning(
                    "[TRACE:capability] SESSION_CLOSE_FAILED capability=%s error=%s",
                    cap.capability_id, error,
                )
            meta["disabled_at"] = _iso(_utcnow())
            meta["last_error"] = ""
            self._append_history(definition, "disable", "active", "disabled", True, "")
            cap.definition = definition
            cap.state = "disabled"
            self.db.flush()
            self.db.commit()
            return _result(
                capability_id=cap.capability_id, action="disable", ok=True,
                state=cap.state, changed=True,
                details={
                    "unregistered": removed,
                    "in_flight": self._in_flight_actions(cap),
                },
            )

    # ------------------------------------------------------------------
    # 升级 / 回滚
    # ------------------------------------------------------------------

    def upgrade(
        self,
        capability_id: str,
        target_version: str = "",
        *,
        launch_args: list[str] | None = None,
        env_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """升级到指定版本：装到版本化目录 → 真实验证 → 原子切换定义与注册。

        失败时旧版本的定义、注册与状态保持不变（"只回滚版本号"是禁止的）。
        """
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="upgrade", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="upgrade", ok=False,
                    error="capability_not_found", error_type="not_found",
                )
            old_definition = copy.deepcopy(cap.definition or {})
            launch = old_definition.get("launch")
            if not isinstance(launch, dict):
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=False,
                    state=cap.state, error="capability_not_installed",
                    error_type="not_installed",
                )
            current_version = str(old_definition.get("version", ""))
            resolved = target_version.strip() or self._catalog_version(cap.name)
            if not resolved:
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=False,
                    state=cap.state, error="target_version_required",
                    error_type="invalid_request",
                )
            if resolved == current_version:
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=True,
                    state=cap.state,
                    details={"already_at_version": resolved},
                )

            package_ref = str(old_definition.get("package_ref", ""))
            if not package_ref:
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=False,
                    state=cap.state, error="missing package_ref",
                    error_type="not_installed",
                )
            transport = str(old_definition.get("transport", "stdio"))
            declared = [
                str(item) for item in (old_definition.get("declared_tools") or [])
            ]
            # 升级只替换版本相关字段，用户显式配置（风险/信任/启动参数/环境键）保留。
            preserved = {
                key: value
                for key, value in old_definition.items()
                if key not in {"launch", "tools", "lifecycle", "package_ref",
                               "version", "transport", "declared_tools"}
            }
            staging_dir = (
                self.sandbox_root
                / f"{sanitize_dir_name(cap.name)}{_VERSION_DIR_SEPARATOR}{resolved}"
            )
            staged = installer_mod.install_to_dir(
                candidate_name=cap.name,
                package_ref=package_ref,
                version=resolved,
                transport=transport,
                declared_tools=declared,
                definition=preserved,
                target_dir=staging_dir,
                launch_args=(
                    launch_args if launch_args is not None
                    else [str(item) for item in (launch.get("args") or [])]
                ),
                env_keys=(
                    env_keys if env_keys is not None
                    else [str(item) for item in (launch.get("env_keys") or [])]
                ),
            )
            if not staged.get("ok"):
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=False,
                    state=cap.state, error=str(staged.get("error") or "install failed"),
                    error_type="install_failed",
                )

            new_definition = copy.deepcopy(staged["definition"])
            probe = self._probe_staged(cap, new_definition)
            if not probe.get("ok"):
                self._remove_tree(staging_dir)
                return _result(
                    capability_id=cap.capability_id, action="upgrade", ok=False,
                    state=cap.state,
                    error=str(probe.get("error") or "verification failed"),
                    error_type="verification_failed",
                    details={
                        "target_version": resolved,
                        "rolled_back": False,
                        "hint": "新版本未通过验证，旧版本定义与注册保持不变",
                    },
                )
            new_definition["tools"] = probe.get("tools") or []

            old_state = cap.state
            old_meta = dict(old_definition.get("lifecycle") or {})
            new_meta = dict(old_meta)
            new_meta["verified_at"] = _iso(_utcnow())
            new_meta["verified_fingerprint"] = _definition_fingerprint(new_definition)
            new_meta["previous_version"] = current_version
            new_meta["last_error"] = ""
            new_definition["lifecycle"] = new_meta

            # 旧版本快照写进版本记录，回滚以它为准（不只是版本号）。
            old_descriptor_hash = cap.descriptor_hash
            self._snapshot_release(cap, old_definition, old_state)
            target_state = (
                "active" if old_state == "active"
                else "disabled" if old_state == "disabled"
                else "verified"
            )
            swapped = True
            activation: dict[str, Any] = {"ok": False, "error": "", "registered": []}
            if old_state == "active":
                try:
                    # 旧定义仍在 cap 上，反注册按旧工具 id 进行。
                    unregister_capability_tools(self._registry(), cap)
                    try:
                        self._client().close(cap.capability_id)
                    except Exception:
                        logger.warning(
                            "[TRACE:capability] SESSION_CLOSE_FAILED capability=%s",
                            cap.capability_id,
                        )
                    # 注册必须按新版本定义进行：先切定义，再激活工具。
                    cap.definition = copy.deepcopy(new_definition)
                    cap.descriptor_hash = str(staged["descriptor_hash"])
                    activation = activate_capability_tools(self._registry(), cap)
                except Exception as error:
                    activation = {"ok": False, "error": str(error), "registered": []}
                if activation.get("ok"):
                    new_meta["registered_tools"] = [
                        str(item) for item in activation.get("registered", [])
                    ]
                if not activation.get("ok"):
                    swapped = False
                    # 恢复旧定义 + 旧注册 + 旧状态，避免"版本号已变但工具仍是新的"。
                    cap.definition = copy.deepcopy(old_definition)
                    cap.descriptor_hash = old_descriptor_hash
                    cap.state = old_state
                    restored = activate_capability_tools(self._registry(), cap)
                    self._remove_tree(staging_dir)
                    self.db.flush()
                    self.db.commit()
                    return _result(
                        capability_id=cap.capability_id, action="upgrade", ok=False,
                        state=cap.state,
                        error=str(activation.get("error") or "registration failed"),
                        error_type="registration_failed",
                        details={
                            "target_version": resolved,
                            "rolled_back": True,
                            "restored_registration": bool(restored.get("ok")),
                        },
                    )

            ensure_transition(old_state, target_state)
            cap.state = target_state
            cap.descriptor_hash = str(staged["descriptor_hash"])
            if target_state != "active":
                # 未启用状态下不保留"已注册"假象：工具当前不在发现索引里。
                new_meta["registered_tools"] = []
            new_meta["upgraded_at"] = _iso(_utcnow())
            if target_state == "active":
                new_meta["registered_at"] = new_meta["upgraded_at"]
                new_meta["enabled_at"] = new_meta.get("enabled_at") or new_meta["upgraded_at"]
            self._append_history(
                new_definition, "upgrade", old_state, target_state, True, "",
                version_from=current_version, version_to=resolved,
            )
            cap.definition = copy.deepcopy(new_definition)
            self._record_version(
                cap, resolved, staged, target_state,
                new_definition, probe, sandbox_path=str(staged["sandbox_path"]),
            )
            self.db.flush()
            self.db.commit()
            return _result(
                capability_id=cap.capability_id, action="upgrade", ok=True,
                state=cap.state, changed=swapped,
                details={
                    "from_version": current_version,
                    "to_version": resolved,
                    "sandbox_path": str(staged["sandbox_path"]),
                    "rolled_back": False,
                },
            )

    def rollback(self, capability_id: str, target_version: str = "") -> dict[str, Any]:
        """回滚到上一个（或指定）版本：恢复定义、注册与状态，三者一起回退。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="rollback", ok=False,
                error="capability_not_found", error_type="not_found",
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="rollback", ok=False,
                    error="capability_not_found", error_type="not_found",
                )
            current = copy.deepcopy(cap.definition or {})
            current_version = str(current.get("version", ""))
            releases = [
                release for release in self._versions(cap)
                if release.version != current_version
                and isinstance(release.definition_snapshot, dict)
                and release.definition_snapshot
            ]
            if target_version:
                releases = [r for r in releases if r.version == target_version]
            if not releases:
                return _result(
                    capability_id=cap.capability_id, action="rollback", ok=False,
                    state=cap.state, error="no_previous_version",
                    error_type="no_previous_version",
                )
            release = releases[0]
            snapshot = copy.deepcopy(release.definition_snapshot or {})
            if not isinstance(snapshot.get("launch"), dict):
                return _result(
                    capability_id=cap.capability_id, action="rollback", ok=False,
                    state=cap.state, error="previous_version_snapshot_incomplete",
                    error_type="snapshot_incomplete",
                )
            launch = snapshot["launch"]
            sandbox_path = launch.get("sandbox_path")
            if isinstance(sandbox_path, str) and sandbox_path and not Path(sandbox_path).exists():
                return _result(
                    capability_id=cap.capability_id, action="rollback", ok=False,
                    state=cap.state, error="previous_version_artifacts_missing",
                    error_type="artifact_missing",
                )

            old_state = cap.state
            restore_state = (
                "active" if old_state == "active"
                else "disabled" if old_state == "disabled"
                else "verified"
            )
            meta = _lifecycle_block(snapshot)
            meta["rolled_back_at"] = _iso(_utcnow())
            meta["last_error"] = ""
            meta["previous_version"] = current_version
            if old_state == "active":
                # 反注册必须发生在切换定义之前：工具 id 由当前（新版本）定义算出。
                unregister_capability_tools(self._registry(), cap)
                try:
                    self._client().close(cap.capability_id)
                except Exception:
                    logger.warning(
                        "[TRACE:capability] SESSION_CLOSE_FAILED capability=%s",
                        cap.capability_id,
                    )
            cap.definition = snapshot
            cap.descriptor_hash = str(release.descriptor_hash or "") or None

            if old_state == "active":
                activation = activate_capability_tools(self._registry(), cap)
                if not activation.get("ok"):
                    # 回滚失败必须停在"旧定义 + 未启用"的确定状态，不能半新半旧。
                    cap.state = "verified"
                    self.db.flush()
                    self.db.commit()
                    return _result(
                        capability_id=cap.capability_id, action="rollback", ok=False,
                        state=cap.state,
                        error=str(activation.get("error") or "registration failed"),
                        error_type="registration_failed",
                        details={"restored_version": release.version},
                    )

            ensure_transition(old_state, restore_state)
            cap.state = restore_state
            if restore_state == "active":
                meta["enabled_at"] = meta.get("enabled_at") or _iso(_utcnow())
                meta["registered_at"] = _iso(_utcnow())
            self._append_history(
                snapshot, "rollback", old_state, restore_state, True, "",
                version_from=current_version, version_to=release.version,
            )
            cap.definition = snapshot
            self._record_version(
                cap, release.version, None, restore_state, snapshot, None,
                sandbox_path=str(sandbox_path or ""),
                descriptor_hash=str(release.descriptor_hash or ""),
                tool_list_hash=str(release.tool_list_hash or ""),
            )
            self.db.flush()
            self.db.commit()
            return _result(
                capability_id=cap.capability_id, action="rollback", ok=True,
                state=cap.state, changed=True,
                details={
                    "from_version": current_version,
                    "to_version": release.version,
                },
            )

    # ------------------------------------------------------------------
    # 卸载
    # ------------------------------------------------------------------

    def uninstall(self, capability_id: str, *, force: bool = False) -> dict[str, Any]:
        """卸载 MCP 能力：检查占用 → 清注册/会话/记录/产物（严格限定范围）。"""
        cap = self._find(capability_id)
        if cap is None:
            return _result(
                capability_id=capability_id, action="uninstall", ok=True,
                details={"not_installed": True},
            )
        with _capability_lock(cap.capability_id):
            cap = self._find_locked(cap.capability_id)
            if cap is None:
                return _result(
                    capability_id=capability_id, action="uninstall", ok=True,
                    details={"not_installed": True},
                )
            if not cap.capability_id.startswith("mcp:"):
                # 内置工具与桌面节点能力由各自机制管理，不能走 MCP 卸载路径。
                return _result(
                    capability_id=cap.capability_id, action="uninstall", ok=False,
                    state=cap.state, error="capability_not_uninstallable",
                    error_type="not_uninstallable",
                    details={"hint": "内置工具随版本发布；桌面节点能力由节点连接管理"},
                )
            in_flight = self._in_flight_actions(cap)
            referencing = self._referencing_tasks(cap)
            if (in_flight or referencing) and not force:
                return _result(
                    capability_id=cap.capability_id, action="uninstall", ok=False,
                    state=cap.state, error="capability_in_use",
                    error_type="in_use",
                    details={
                        "in_flight": in_flight,
                        "referencing_tasks": referencing,
                        "hint": "等待任务结束、停用后重试，或显式 force",
                    },
                )

            unregistered = unregister_capability_tools(self._registry(), cap)
            try:
                self._client().close(cap.capability_id)
            except Exception as error:
                logger.warning(
                    "[TRACE:capability] SESSION_CLOSE_FAILED capability=%s error=%s",
                    cap.capability_id, error,
                )
            paths = self._owned_sandbox_paths(cap)
            versions = self._versions(cap)
            installs = self._installs(cap)
            for record in installs:
                self.db.delete(record)
            for version in versions:
                self.db.delete(version)
            self.db.delete(cap)
            self.db.flush()
            self.db.commit()

            removed_paths: list[str] = []
            for path in paths:
                if self._remove_tree(path):
                    removed_paths.append(str(path))
            logger.info(
                "[TRACE:capability] UNINSTALLED capability=%s tools=%s paths=%s",
                capability_id, unregistered, removed_paths,
            )
            return _result(
                capability_id=capability_id, action="uninstall", ok=True,
                changed=True,
                details={
                    "unregistered": unregistered,
                    "removed_paths": removed_paths,
                    "removed_versions": [v.version for v in versions],
                    "forced": bool(force and (in_flight or referencing)),
                },
            )

    # ------------------------------------------------------------------
    # 一致性核查
    # ------------------------------------------------------------------

    def audit(self) -> dict[str, Any]:
        """核对数据库 / 注册表 / 文件系统 / 发现索引之间的一致性。"""
        issues: list[dict[str, str]] = []
        registry = self._registry()
        registered_mcp = {
            entry["capability_id"]: entry
            for entry in registry.list_all()
            if str(entry.get("capability_id", "")).startswith("mcp_")
        }
        caps = self.db.query(Capability).all()
        known_tools: set[str] = set()
        for cap in caps:
            definition = copy.deepcopy(cap.definition or {})
            tool_ids = registered_tool_ids(cap)
            known_tools.update(tool_ids)
            launch = definition.get("launch")
            sandbox_path = (
                str(launch.get("sandbox_path", "")) if isinstance(launch, dict) else ""
            )
            if cap.state == "active":
                missing = [t for t in tool_ids if t not in registered_mcp]
                if missing:
                    issues.append({
                        "kind": "active_not_registered",
                        "capability_id": cap.capability_id,
                        "detail": ",".join(missing),
                    })
                if sandbox_path and not Path(sandbox_path).exists():
                    issues.append({
                        "kind": "missing_artifacts",
                        "capability_id": cap.capability_id,
                        "detail": sandbox_path,
                    })
            if cap.state in ("disabled", "needs_review", "verified"):
                still = [t for t in tool_ids if t in registered_mcp]
                if still:
                    issues.append({
                        "kind": "inactive_still_registered",
                        "capability_id": cap.capability_id,
                        "detail": ",".join(still),
                    })
            versions = self._versions(cap)
            if versions:
                latest = versions[0]
                declared_version = str(definition.get("version", ""))
                if declared_version and latest.version != declared_version:
                    issues.append({
                        "kind": "version_mismatch",
                        "capability_id": cap.capability_id,
                        "detail": f"{latest.version} != {declared_version}",
                    })
        for tool_id in registered_mcp:
            if tool_id not in known_tools:
                issues.append({
                    "kind": "orphan_registration",
                    "capability_id": "",
                    "detail": tool_id,
                })
        return {
            "ok": not issues,
            "issues": issues,
            "counts": {
                "capabilities": len(caps),
                "registered_mcp_tools": len(registered_mcp),
                "issues": len(issues),
            },
        }

    def reconcile(self) -> dict[str, Any]:
        """按核查结果修复可安全自动修复的漂移（不动文件、不删记录）。"""
        actions: list[dict[str, str]] = []
        report = self.audit()
        registry = self._registry()
        caps = {cap.capability_id: cap for cap in self.db.query(Capability).all()}
        for issue in report["issues"]:
            kind = issue["kind"]
            if kind == "orphan_registration":
                if registry.unregister(issue["detail"]):
                    actions.append({"kind": kind, "detail": issue["detail"], "result": "unregistered"})
                continue
            cap = caps.get(issue["capability_id"])
            if cap is None:
                continue
            if kind == "active_not_registered":
                activation = activate_capability_tools(registry, cap)
                actions.append({
                    "kind": kind,
                    "detail": cap.capability_id,
                    "result": "registered" if activation.get("ok") else "failed",
                })
            elif kind == "inactive_still_registered":
                removed = unregister_capability_tools(registry, cap)
                actions.append({
                    "kind": kind,
                    "detail": cap.capability_id,
                    "result": f"unregistered:{len(removed)}",
                })
            elif kind == "missing_artifacts":
                definition = copy.deepcopy(cap.definition or {})
                self._mark_needs_review(cap, definition, "sandbox artifacts missing")
                actions.append({
                    "kind": kind,
                    "detail": cap.capability_id,
                    "result": "needs_review",
                })
        self.db.flush()
        self.db.commit()
        return {"ok": True, "actions": actions, "remaining": self.audit()["issues"]}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _catalog_version(self, name: str) -> str:
        """从内置目录取该能力的固定版本（升级默认目标版本）。"""
        from aiive.mcp.discovery import get_candidate_by_name

        candidate = get_candidate_by_name(name)
        return candidate.version if candidate is not None else ""

    def _probe_staged(
        self, cap: Capability, definition: dict[str, Any],
    ) -> dict[str, Any]:
        """在切换前验证新版本：独立会话键，不触碰正在运行的旧版本。"""
        from aiive.mcp.runtime_client import build_launch_spec

        launch = definition.get("launch")
        if not isinstance(launch, dict):
            return {"ok": False, "error": "missing launch config"}
        staging_key = f"{cap.capability_id}#staging"
        client = self._client()
        try:
            client.configure(staging_key, build_launch_spec(launch))
            tools = client.list_tools(staging_key)
        except Exception as error:
            return {"ok": False, "error": str(error)}
        finally:
            try:
                client.close(staging_key)
            except Exception:
                pass
        names = [str(tool.get("name", "")) for tool in tools]
        if not names:
            return {"ok": False, "error": "server started but exposes no tools"}
        return {"ok": True, "error": None, "tools": tools, "real_tools": names}

    def _snapshot_release(
        self, cap: Capability, definition: dict[str, Any], state: str,
    ) -> None:
        """把当前（即将被替换的）版本定义快照写进版本记录，供回滚使用。"""
        version = str(definition.get("version", ""))
        if not version:
            return
        launch = definition.get("launch")
        sandbox_path = (
            str(launch.get("sandbox_path", "")) if isinstance(launch, dict) else ""
        )
        row = (
            self.db.query(CapabilityVersion)
            .filter(
                CapabilityVersion.capability_id == cap.id,
                CapabilityVersion.version == version,
            )
            .order_by(CapabilityVersion.created_at.desc())
            .first()
        )
        if row is None:
            row = CapabilityVersion(
                capability_id=cap.id,
                version=version,
                descriptor_hash=cap.descriptor_hash or "",
                tool_list_hash=None,
                smoke_result=None,
            )
            self.db.add(row)
        row.definition_snapshot = copy.deepcopy(definition)
        row.state = state
        row.sandbox_path = sandbox_path
        row.source = str(definition.get("source", ""))
        self.db.flush()

    def _record_version(
        self,
        cap: Capability,
        version: str,
        staged: dict[str, Any] | None,
        state: str,
        definition: dict[str, Any],
        probe: dict[str, Any] | None,
        *,
        sandbox_path: str,
        descriptor_hash: str = "",
        tool_list_hash: str = "",
    ) -> None:
        """新增一条版本记录（升级成功 / 回滚都会留下可追溯的版本行）。"""
        launch = definition.get("launch")
        effective_sandbox = sandbox_path or (
            str(launch.get("sandbox_path", "")) if isinstance(launch, dict) else ""
        )
        row = CapabilityVersion(
            capability_id=cap.id,
            version=version,
            descriptor_hash=descriptor_hash or str((staged or {}).get("descriptor_hash", "")),
            tool_list_hash=tool_list_hash or str((staged or {}).get("tool_list_hash", "")),
            smoke_result=(
                {"ok": bool(probe.get("ok")), "mode": "tools_list",
                 "real_tools": probe.get("real_tools") or []}
                if probe is not None else None
            ),
            source=str(definition.get("source", "")),
            state=state,
            sandbox_path=effective_sandbox,
            definition_snapshot=copy.deepcopy(definition),
        )
        self.db.add(row)
        self.db.flush()

    def _mark_needs_review(
        self, cap: Capability, definition: dict[str, Any], error: str,
    ) -> None:
        """把能力推进到 needs_review（从 active/verified/sandbox 均可）。"""
        meta = _lifecycle_block(definition)
        meta["last_error"] = error
        before = cap.state
        target = "needs_review"
        try:
            ensure_transition(before, target)
        except InvalidCapabilityTransition:
            target = before
        if target != before:
            cap.state = target
            unregister_capability_tools(self._registry(), cap)
        self._append_history(definition, "mark_needs_review", before, cap.state, False, error)
        cap.definition = definition
        self.db.flush()
        self.db.commit()

    def _append_history(
        self,
        definition: dict[str, Any],
        action: str,
        from_state: str,
        to_state: str,
        ok: bool,
        error: str,
        *,
        version_from: str = "",
        version_to: str = "",
    ) -> None:
        """追加一条有界生命周期历史（最近 _HISTORY_LIMIT 条）。"""
        meta = _lifecycle_block(definition)
        history = meta.get("history")
        if not isinstance(history, list):
            history = []
        entry: dict[str, Any] = {
            "at": _iso(_utcnow()),
            "action": action,
            "from": from_state,
            "to": to_state,
            "ok": ok,
        }
        if error:
            entry["error"] = error
        if version_from:
            entry["version_from"] = version_from
        if version_to:
            entry["version_to"] = version_to
        history.append(entry)
        meta["history"] = history[-_HISTORY_LIMIT:]
        meta["last_action"] = action
        meta["updated_at"] = entry["at"]

    def _owned_sandbox_paths(self, cap: Capability) -> list[Path]:
        """收集该能力名下的沙箱目录（含版本化目录），去重且限定在沙箱根内。"""
        raw_paths: set[str] = set()
        definition = copy.deepcopy(cap.definition or {})
        launch = definition.get("launch")
        if isinstance(launch, dict) and isinstance(launch.get("sandbox_path"), str):
            raw_paths.add(str(launch["sandbox_path"]))
        for record in self._installs(cap):
            if record.sandbox_path:
                raw_paths.add(str(record.sandbox_path))
        for version in self._versions(cap):
            if version.sandbox_path:
                raw_paths.add(str(version.sandbox_path))
            snapshot: dict[str, Any] = version.definition_snapshot or {}
            snap_launch = snapshot.get("launch")
            if isinstance(snap_launch, dict) and isinstance(snap_launch.get("sandbox_path"), str):
                raw_paths.add(str(snap_launch["sandbox_path"]))

        owned: list[Path] = []
        root = self.sandbox_root.resolve()
        base = sanitize_dir_name(cap.name)
        for raw in raw_paths:
            try:
                path = Path(raw).resolve()
            except OSError:
                continue
            if path == root or root not in path.parents:
                logger.warning(
                    "[TRACE:capability] UNSAFE_PATH_SKIPPED capability=%s path=%s",
                    cap.capability_id, raw,
                )
                continue
            name = path.name
            if name != base and not name.startswith(f"{base}{_VERSION_DIR_SEPARATOR}"):
                logger.warning(
                    "[TRACE:capability] FOREIGN_PATH_SKIPPED capability=%s path=%s",
                    cap.capability_id, raw,
                )
                continue
            if path not in owned:
                owned.append(path)
        return owned

    def _remove_tree(self, path: Path) -> bool:
        """删除单个沙箱目录；越界路径一律拒绝，绝不递归删沙箱根。"""
        try:
            target = Path(path).resolve()
        except OSError:
            return False
        root = self.sandbox_root.resolve()
        if target == root or root not in target.parents:
            logger.warning("[TRACE:capability] REMOVE_OUTSIDE_ROOT path=%s", target)
            return False
        if not target.exists():
            return False
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
