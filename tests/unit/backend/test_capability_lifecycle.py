"""能力生命周期测试：启用/停用、升级/回滚、卸载、幂等与一致性核查。

全部离线：npm 安装、MCP 运行时、ToolRegistry 都被替换为进程内桩，
不调用真实外部 API、不需要 API Key、不消耗第三方额度。
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiive.capability import lifecycle as lifecycle_mod
from aiive.capability.lifecycle import CapabilityLifecycleService, _capability_lock
from aiive.capability.states import (
    InvalidCapabilityTransition,
    ensure_transition,
    parse_state,
)
from aiive.control.capability_broker import CapabilityBroker
from aiive.db.models import (
    AgentAction,
    AgentRun,
    AgentTask,
    Capability,
    CapabilityVersion,
    MCPInstallRecord,
    Thread,
)
from aiive.mcp import discovery as discovery_mod
from aiive.mcp import installer as installer_mod
from aiive.mcp import runtime_client as runtime_client_mod
from aiive.mcp.bootstrap import mcp_tool_capability_id
from aiive.mcp.installer import install_sandbox
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
)

_SERVER = "lifecycle-test-server"
_OTHER = "lifecycle-other-server"
_TOOLS = ["read_graph", "write_graph"]


class _FakeSpec:
    """替代 MCPLaunchSpec：只保留测试关心的入口路径。"""

    def __init__(self, entry: str) -> None:
        self.entry = entry


class FakeRuntime:
    """进程内 MCP 运行时桩：按入口路径里的版本号返回不同工具集。"""

    def __init__(self) -> None:
        self.tools_by_version: dict[str, list[str]] = {}
        self.default_tools: list[str] = list(_TOOLS)
        self.fail_versions: set[str] = set()
        self.configured: dict[str, _FakeSpec] = {}
        self.closed: list[str] = []

    def configure(self, capability_id: str, spec: _FakeSpec) -> None:
        self.configured[capability_id] = spec

    def _version_of(self, capability_id: str) -> str:
        spec = self.configured.get(capability_id)
        entry = str(getattr(spec, "entry", ""))
        for version in self.tools_by_version:
            if f"@{version}" in entry:
                return version
        return ""

    def list_tools(self, capability_id: str) -> list[dict[str, object]]:
        version = self._version_of(capability_id)
        if version in self.fail_versions:
            raise RuntimeError(f"spawn failed for {version or 'current'}")
        names = self.tools_by_version.get(version, self.default_tools)
        return [
            {"name": name, "description": name,
             "input_schema": {"type": "object", "properties": {}}}
            for name in names
        ]

    def call_tool(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("生命周期测试不应调用 tools/call")

    def close(self, capability_id: str | None = None) -> None:
        self.closed.append(capability_id or "")


@pytest.fixture
def env(db_session, monkeypatch, tmp_path):
    """隔离的沙箱根目录 + 假 npm + 假运行时 + 干净注册表。"""
    root = tmp_path / "mcp_sandbox"
    root.mkdir()
    monkeypatch.setattr(installer_mod, "SANDBOX_ROOT", root)
    monkeypatch.setattr(
        discovery_mod, "allowed_npm_packages", lambda: {_SERVER, _OTHER},
    )

    def _fake_npm(package_name: str, version: str, sandbox_dir: Path):
        directory = Path(sandbox_dir)
        directory.mkdir(parents=True, exist_ok=True)
        entry = directory / "index.js"
        entry.write_text("// fake mcp entry", encoding="utf-8")
        return {
            "ok": True, "entry_js": str(entry), "error": None,
            "sandbox_path": str(directory),
        }

    monkeypatch.setattr(installer_mod, "_run_npm_install", _fake_npm)
    monkeypatch.setattr(
        runtime_client_mod, "build_launch_spec",
        lambda launch: _FakeSpec(str(launch.get("entry_js", ""))),
    )
    runtime = FakeRuntime()
    registry = ToolRegistry()

    def service() -> CapabilityLifecycleService:
        return CapabilityLifecycleService(
            db_session, registry=registry, client=runtime, sandbox_root=root,
        )

    return SimpleNamespace(
        db=db_session, root=root, runtime=runtime, registry=registry,
        service=service,
    )


def _install(env, name: str = _SERVER, version: str = "1.0.0") -> Capability:
    result = install_sandbox(
        env.db, name, f"npm:{name}", version, "stdio", list(_TOOLS),
        {"name": name, "source": "official_reference",
         "description": "lifecycle test server", "trust_level": "semi_trusted"},
    )
    env.db.flush()
    assert result["ok"] is True, result
    cap = (
        env.db.query(Capability)
        .filter(Capability.capability_id == f"mcp:{name}")
        .one()
    )
    return cap


def _tool_id(name: str = _SERVER, tool: str = "read_graph") -> str:
    return mcp_tool_capability_id(name, tool)


def _activate(env, name: str = _SERVER) -> Capability:
    """安装 → 验证 → 启用，返回最新能力行。"""
    _install(env, name)
    service = env.service()
    assert service.verify(f"mcp:{name}")["ok"] is True
    assert service.enable(f"mcp:{name}")["ok"] is True
    return (
        env.db.query(Capability)
        .filter(Capability.capability_id == f"mcp:{name}")
        .one()
    )


class TestStateMachine:
    """状态机是唯一裁决处：非法转换必须报错，不能静默改状态。"""

    def test_illegal_transitions_rejected(self) -> None:
        with pytest.raises(InvalidCapabilityTransition):
            ensure_transition("candidate", "active")
        with pytest.raises(InvalidCapabilityTransition):
            ensure_transition("active", "verified")
        with pytest.raises(InvalidCapabilityTransition):
            parse_state("bogus")

    def test_legal_transitions_allowed(self) -> None:
        ensure_transition("sandbox", "verified")
        ensure_transition("verified", "active")
        ensure_transition("active", "disabled")
        ensure_transition("disabled", "active")
        ensure_transition("active", "active")  # 同状态是幂等空操作


class TestInstallVerifyEnable:
    """安装成功、验证通过、注册成功、正式启用是四个独立事实。"""

    def test_install_is_not_verification(self, env) -> None:
        cap = _install(env)
        assert cap.state == "sandbox"
        details = env.service().status(cap.capability_id)["details"]
        assert details["installed"] is True
        assert details["verified"] is False
        assert details["registered"] is False
        assert details["enabled"] is False

    def test_verify_does_not_enable(self, env) -> None:
        cap = _install(env)
        result = env.service().verify(cap.capability_id)
        assert result["ok"] is True
        assert result["state"] == "verified"
        assert env.registry.get(_tool_id()) is None  # 验证不产生发现索引
        details = env.service().status(cap.capability_id)["details"]
        assert details["verified"] is True
        assert details["enabled"] is False

    def test_enable_registers_and_activates(self, env) -> None:
        cap = _install(env)
        service = env.service()
        service.verify(cap.capability_id)
        result = service.enable(cap.capability_id)
        assert result["ok"] is True
        assert result["state"] == "active"
        assert set(result["details"]["registered"]) == {
            _tool_id(), _tool_id(tool="write_graph"),
        }
        assert env.registry.get(_tool_id()) is not None
        details = service.status(cap.capability_id)["details"]
        assert details["registered"] is True
        assert details["enabled"] is True
        # 注册与启用各自留痕，不是同一个时间戳糊在一起
        assert details["lifecycle"]["registered_at"]
        assert details["lifecycle"]["enabled_at"]

    def test_enable_requires_verification(self, env) -> None:
        cap = _install(env)
        result = env.service().enable(cap.capability_id)
        assert result["ok"] is False
        assert result["error_type"] == "invalid_state"
        assert env.registry.get(_tool_id()) is None

    def test_definition_change_requires_reverify(self, env) -> None:
        cap = _install(env)
        service = env.service()
        service.verify(cap.capability_id)
        definition = dict(cap.definition or {})
        definition["description"] = "tampered after verification"
        cap.definition = definition
        env.db.flush()
        result = service.enable(cap.capability_id)
        assert result["ok"] is False
        assert result["error_type"] == "definition_changed"
        # 重新验证后可以启用
        assert service.verify(cap.capability_id)["ok"] is True
        assert service.enable(cap.capability_id)["ok"] is True

    def test_enable_is_idempotent(self, env) -> None:
        cap = _activate(env)
        result = env.service().enable(cap.capability_id)
        assert result["ok"] is True
        assert result["changed"] is False
        assert result["details"]["already_enabled"] is True
        assert env.db.query(CapabilityVersion).count() == 1


class TestDisable:
    """停用只影响"能否被发现"，保留安装产物与用户配置。"""

    def test_disable_removes_from_discovery(self, env) -> None:
        cap = _activate(env)
        before = dict(cap.definition or {})
        result = env.service().disable(cap.capability_id)
        assert result["ok"] is True
        assert result["state"] == "disabled"
        assert env.registry.get(_tool_id()) is None
        assert env.registry.search("read_graph") == []
        env.db.refresh(cap)
        after = dict(cap.definition or {})
        # 用户配置与安装信息原样保留
        assert after["launch"] == before["launch"]
        assert after["trust_level"] == before["trust_level"]
        assert after["tools"] == before["tools"]
        assert Path(after["launch"]["sandbox_path"]).exists()

    def test_disable_is_idempotent(self, env) -> None:
        cap = _activate(env)
        service = env.service()
        service.disable(cap.capability_id)
        second = service.disable(cap.capability_id)
        assert second["ok"] is True
        assert second["changed"] is False
        assert second["details"]["already_disabled"] is True

    def test_disable_then_enable_restores_registration(self, env) -> None:
        cap = _activate(env)
        service = env.service()
        service.disable(cap.capability_id)
        assert env.registry.get(_tool_id()) is None
        result = service.enable(cap.capability_id)
        assert result["ok"] is True
        assert env.registry.get(_tool_id()) is not None

    def test_dispatch_rejects_disabled_capability(self, env) -> None:
        """已停用能力的在途 Action 派发时得到确定的 capability_disabled。"""
        cap = _activate(env)
        env.service().disable(cap.capability_id)
        task, run, action = _seed_running_action(env.db, _tool_id())
        result = CapabilityBroker(env.db).dispatch(task, run, action)
        assert result["ok"] is False
        assert result["error_type"] == "capability_disabled"

    def test_capability_lock_serializes_same_capability(self) -> None:
        """同一能力的写操作必须串行：持锁期间其他线程进不来。"""
        lock = _capability_lock("mcp:serialize-me")
        assert lock is _capability_lock("mcp:serialize-me")
        entered = threading.Event()

        def _worker() -> None:
            with _capability_lock("mcp:serialize-me"):
                entered.set()

        worker = threading.Thread(target=_worker)
        with _capability_lock("mcp:serialize-me"):
            worker.start()
            assert entered.wait(0.2) is False
        worker.join(timeout=3)
        assert entered.is_set()
        assert _capability_lock("mcp:other") is not lock

    def test_broker_distinguishes_disabled_from_unknown(self, env) -> None:
        cap = _activate(env)
        env.service().disable(cap.capability_id)
        broker = CapabilityBroker(env.db)
        assert broker._is_disabled(capability_id=_tool_id()) is True
        assert broker._is_disabled(capability_id="mcp_never_existed_tool") is False


class TestUpgrade:
    """升级：版本化目录 + 切换前验证 + 原子切换，失败保留旧定义与注册。"""

    def test_upgrade_success_switches_version_and_tools(self, env) -> None:
        cap = _activate(env)
        old_dir = Path(cap.definition["launch"]["sandbox_path"])
        env.runtime.tools_by_version["2.0.0"] = [*_TOOLS, "extra_tool"]
        result = env.service().upgrade(cap.capability_id, "2.0.0")
        assert result["ok"] is True, result
        assert result["state"] == "active"
        assert result["details"]["from_version"] == "1.0.0"
        assert result["details"]["to_version"] == "2.0.0"
        env.db.refresh(cap)
        assert cap.definition["version"] == "2.0.0"
        assert env.registry.get(_tool_id(tool="extra_tool")) is not None
        assert old_dir.exists()  # 旧版本目录保留，供回滚使用
        assert Path(cap.definition["launch"]["sandbox_path"]).exists()
        assert env.db.query(CapabilityVersion).count() >= 2

    def test_upgrade_same_version_is_idempotent(self, env) -> None:
        cap = _activate(env)
        result = env.service().upgrade(cap.capability_id, "1.0.0")
        assert result["ok"] is True
        assert result["changed"] is False
        assert result["details"]["already_at_version"] == "1.0.0"

    def test_upgrade_verification_failure_keeps_old(self, env) -> None:
        cap = _activate(env)
        env.runtime.tools_by_version["2.0.0"] = [*_TOOLS]
        env.runtime.fail_versions.add("2.0.0")
        result = env.service().upgrade(cap.capability_id, "2.0.0")
        assert result["ok"] is False
        assert result["error_type"] == "verification_failed"
        env.db.refresh(cap)
        assert cap.definition["version"] == "1.0.0"
        assert cap.state == "active"
        assert env.registry.get(_tool_id()) is not None
        # 未通过验证的版本目录已清理，不留半装状态
        assert not (env.root / f"{_SERVER}@2.0.0").exists()

    def test_upgrade_registration_failure_rolls_back(self, env, monkeypatch) -> None:
        cap = _activate(env)
        env.runtime.tools_by_version["2.0.0"] = [*_TOOLS, "extra_tool"]
        real = lifecycle_mod.activate_capability_tools
        calls = {"n": 0}

        def _flaky(registry, capability):
            calls["n"] += 1
            if calls["n"] == 1:  # 只让升级路径的新版本注册失败
                return {"ok": False, "registered": [], "error": "registry boom"}
            return real(registry, capability)

        monkeypatch.setattr(lifecycle_mod, "activate_capability_tools", _flaky)
        result = env.service().upgrade(cap.capability_id, "2.0.0")
        assert result["ok"] is False
        assert result["error_type"] == "registration_failed"
        assert result["details"]["rolled_back"] is True
        assert result["details"]["restored_registration"] is True
        env.db.refresh(cap)
        assert cap.definition["version"] == "1.0.0"
        assert cap.state == "active"
        assert env.registry.get(_tool_id()) is not None
        assert env.registry.get(_tool_id(tool="extra_tool")) is None
        assert not (env.root / f"{_SERVER}@2.0.0").exists()


class TestRollback:
    """回滚必须还原定义、注册与状态，而不是只改版本号。"""

    def test_rollback_restores_previous_release(self, env) -> None:
        cap = _activate(env)
        env.runtime.tools_by_version["2.0.0"] = [*_TOOLS, "extra_tool"]
        assert env.service().upgrade(cap.capability_id, "2.0.0")["ok"] is True
        assert env.registry.get(_tool_id(tool="extra_tool")) is not None

        result = env.service().rollback(cap.capability_id)
        assert result["ok"] is True, result
        assert result["details"]["from_version"] == "2.0.0"
        assert result["details"]["to_version"] == "1.0.0"
        env.db.refresh(cap)
        assert cap.definition["version"] == "1.0.0"
        assert cap.state == "active"
        assert env.registry.get(_tool_id()) is not None
        assert env.registry.get(_tool_id(tool="extra_tool")) is None

    def test_rollback_without_history_fails_closed(self, env) -> None:
        cap = _activate(env)
        result = env.service().rollback(cap.capability_id)
        assert result["ok"] is False
        assert result["error_type"] == "no_previous_version"


class TestUninstall:
    """卸载：先检查占用，再清理注册/记录/产物，且只删自己名下的目录。"""

    def test_uninstall_removes_rows_registration_and_own_artifacts(self, env) -> None:
        cap = _activate(env)
        other = _activate(env, _OTHER)
        own_dir = Path(cap.definition["launch"]["sandbox_path"])
        other_dir = Path(other.definition["launch"]["sandbox_path"])

        result = env.service().uninstall(cap.capability_id)
        assert result["ok"] is True, result
        assert env.registry.get(_tool_id()) is None
        assert env.registry.get(_tool_id(_OTHER)) is not None
        assert other_dir.exists()
        assert not own_dir.exists()
        assert (
            env.db.query(Capability)
            .filter(Capability.capability_id == cap.capability_id)
            .count()
        ) == 0
        assert (
            env.db.query(MCPInstallRecord)
            .filter(MCPInstallRecord.server_name == _SERVER)
            .count()
        ) == 0

    def test_uninstall_rejects_in_flight_action(self, env) -> None:
        cap = _activate(env)
        _seed_running_action(env.db, _tool_id())
        result = env.service().uninstall(cap.capability_id)
        assert result["ok"] is False
        assert result["error_type"] == "in_use"
        assert result["details"]["in_flight"]
        # 拒绝时底层资源必须完好
        assert Path(cap.definition["launch"]["sandbox_path"]).exists()
        assert env.registry.get(_tool_id()) is not None

        forced = env.service().uninstall(cap.capability_id, force=True)
        assert forced["ok"] is True
        assert forced["details"]["forced"] is True

    def test_uninstall_is_idempotent(self, env) -> None:
        cap = _activate(env)
        assert env.service().uninstall(cap.capability_id)["ok"] is True
        second = env.service().uninstall(cap.capability_id)
        assert second["ok"] is True
        assert second["details"]["not_installed"] is True

    def test_uninstall_rejects_non_mcp_capability(self, env) -> None:
        cap = Capability(capability_id="echo", name="echo", state="active", definition={})
        env.db.add(cap)
        env.db.flush()
        result = env.service().uninstall("echo")
        assert result["ok"] is False
        assert result["error_type"] == "not_uninstallable"

    def test_uninstall_never_deletes_outside_sandbox(self, env, tmp_path) -> None:
        cap = _activate(env)
        outside = tmp_path / "user-data"
        outside.mkdir()
        (outside / "important.txt").write_text("keep", encoding="utf-8")
        definition = dict(cap.definition or {})
        definition["launch"] = {**definition["launch"], "sandbox_path": str(outside)}
        cap.definition = definition
        env.db.flush()

        result = env.service().uninstall(cap.capability_id)
        assert result["ok"] is True
        assert outside.exists()
        assert (outside / "important.txt").exists()
        assert str(outside) not in result["details"]["removed_paths"]


class TestConsistency:
    """数据库、注册表与文件系统漂移的核查与修复。"""

    def test_audit_detects_and_reconcile_repairs_registration_drift(self, env) -> None:
        cap = _activate(env)
        env.registry.unregister(_tool_id())
        audit = env.service().audit()
        kinds = {issue["kind"] for issue in audit["issues"]}
        assert "active_not_registered" in kinds

        env.service().reconcile()
        assert env.registry.get(_tool_id()) is not None
        assert env.service().audit()["ok"] is True
        assert cap.state == "active"

    def test_audit_detects_orphan_registration(self, env) -> None:
        env.registry.register(ToolRegistration(
            safety=CapabilitySafetySchema(
                capability_id="mcp_ghost_tool",
                definition_source="remote_mcp",
                definition_trust_level="untrusted",
                risk_level="low",
            ),
            handler=lambda **_: None,
            description="ghost",
        ))
        audit = env.service().audit()
        assert any(
            issue["kind"] == "orphan_registration"
            and issue["detail"] == "mcp_ghost_tool"
            for issue in audit["issues"]
        )
        env.service().reconcile()
        assert env.registry.get("mcp_ghost_tool") is None

    def test_audit_flags_missing_artifacts_and_marks_needs_review(self, env) -> None:
        cap = _activate(env)
        Path(cap.definition["launch"]["sandbox_path"]).rename(env.root / "moved-away")
        audit = env.service().audit()
        assert any(issue["kind"] == "missing_artifacts" for issue in audit["issues"])
        env.service().reconcile()
        env.db.refresh(cap)
        assert cap.state == "needs_review"
        assert env.registry.get(_tool_id()) is None

    def test_history_is_bounded(self, env) -> None:
        cap = _activate(env)
        service = env.service()
        for _ in range(12):
            service.disable(cap.capability_id)
            service.enable(cap.capability_id)
        env.db.refresh(cap)
        history = cap.definition["lifecycle"]["history"]
        assert len(history) <= 20
        assert history[-1]["action"] == "enable"


def _seed_running_action(db, capability_id: str):
    """造一条运行中的 Task/Run/Action，验证占用检查与派发拒绝。"""
    db.add(Thread(id="thread-lifecycle", title="lifecycle"))
    task = AgentTask(
        id="task-lifecycle", thread_id="thread-lifecycle", task_type="general",
        title="running task", goal="use capability", status="running",
        executor_type="agent", priority=0,
        task_brief={"allowed_capabilities": [capability_id]},
        task_state={}, budgets={}, usage={}, version=1,
    )
    db.add(task)
    run = AgentRun(id="run-lifecycle", task_id="task-lifecycle", run_index=1, status="running")
    db.add(run)
    action = AgentAction(
        id="action-lifecycle", task_id="task-lifecycle", run_id="run-lifecycle",
        sequence=1, capability_id=capability_id, arguments={},
        arguments_hash="args-hash", descriptor_hash="desc-hash",
        idempotency_key="idem-lifecycle", status="running",
    )
    db.add(action)
    db.flush()
    return task, run, action


class TestReinstallConsistency:
    """重装把 active 拉回 needs_review 时，发现索引必须同步摘除。"""

    def test_reinstall_of_active_capability_removes_from_discovery(
        self, env, monkeypatch,
    ) -> None:
        from aiive.tools import registry as registry_module

        _activate(env)
        assert env.registry.get(_tool_id()) is not None
        monkeypatch.setattr(registry_module, "get_tool_registry", lambda: env.registry)

        result = install_sandbox(
            env.db, _SERVER, f"npm:{_SERVER}", "1.0.0", "stdio", list(_TOOLS),
            {"name": _SERVER, "source": "official_reference",
             "description": "lifecycle test server", "trust_level": "semi_trusted"},
        )
        env.db.flush()
        assert result["ok"] is True
        assert result["state"] == "needs_review"
        assert env.registry.get(_tool_id()) is None


class TestApiWiring:
    """路由层只做参数/响应转换，服务层注入后即可离线验证。"""

    def test_lifecycle_endpoints_round_trip(self, env, monkeypatch) -> None:
        from aiive.api import routes_capabilities as routes

        monkeypatch.setattr(
            routes, "CapabilityLifecycleService", lambda db: env.service(),
        )
        cap = _activate(env)
        status = routes.capability_lifecycle_status(cap.capability_id, env.db)
        assert status.ok is True
        assert status.details["enabled"] is True

        disabled = routes.disable_capability(cap.capability_id, env.db)
        assert disabled.ok is True
        assert disabled.state == "disabled"

        enabled = routes.enable_capability(cap.capability_id, env.db)
        assert enabled.ok is True
        assert enabled.state == "active"

        audit = routes.audit_capabilities(env.db)
        assert audit["ok"] is True

        upgraded = routes.upgrade_capability(
            cap.capability_id, routes.LifecycleRequest(target_version="1.0.0"), env.db,
        )
        assert upgraded.ok is True
        assert upgraded.details["already_at_version"] == "1.0.0"
