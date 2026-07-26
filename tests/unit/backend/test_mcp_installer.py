"""测试 MCP 安装器——沙箱安装记账、版本管理和冒烟状态机。

installer 现在执行真实 npm 安装（白名单校验 + npm install + bin 入口解析）。
本文件 monkeypatch 掉 npm 执行环节（installer._run_npm_install）与包名白名单
（discovery.allowed_npm_packages），只验证 DB 记账、哈希变更→needs_review、
冒烟状态机等语义；真实 npm/node 链路由
backend/scripts/smoke/smoke_mcp_real.py 手动集成脚本覆盖。
"""
import pytest

from aiive.api.routes_mcp_install import SmokeRequest, smoke_capability
from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord
from aiive.mcp import discovery as discovery_mod
from aiive.mcp import installer as installer_mod
from aiive.mcp.installer import install_sandbox, run_smoke
from aiive.mcp.runtime_client import MCPToolResult

# 各用例使用的测试包名（monkeypatch 进白名单）
_TEST_PACKAGES = {
    "test-server", "test2", "test3", "same", "changed", "test", "srv", "srv2",
}


@pytest.fixture
def fake_npm(monkeypatch, tmp_path):
    """替换真实 npm 执行：返回伪造入口 js；并放行测试包名白名单。"""
    entry = tmp_path / "index.js"
    entry.write_text("// fake mcp server entry", encoding="utf-8")

    def _fake_npm_install(package_name, version, sandbox_dir):
        return {
            "ok": True,
            "entry_js": str(entry),
            "error": None,
            "sandbox_path": str(sandbox_dir),
        }

    monkeypatch.setattr(installer_mod, "_run_npm_install", _fake_npm_install)
    monkeypatch.setattr(
        discovery_mod, "allowed_npm_packages",
        lambda: _TEST_PACKAGES | {"real-catalog-placeholder"},
    )
    return entry


class TestMCPInstaller:
    """测试沙箱安装、重复安装和哈希变更检测。"""

    def test_install_sandbox_creates_capability(self, db_session, fake_npm):
        """验证沙箱安装后创建 Capability 记录且状态为 sandbox。"""
        result = install_sandbox(
            db=db_session,
            candidate_name="test-server",
            package_ref="npm:test-server",
            version="1.0.0",
            transport="stdio",
            declared_tools=["echo", "list_files"],
            definition={"name": "test", "source": "official_registry"},
        )
        db_session.flush()
        assert result["ok"] is True
        assert result["installed"] is True
        assert result["state"] == "sandbox"

        cap = (
            db_session.query(Capability)
            .filter(Capability.capability_id == "mcp:test-server")
            .first()
        )
        assert cap is not None
        assert cap.state == "sandbox"
        # 真实安装的启动信息必须落进 definition
        launch = cap.definition["launch"]
        assert launch["runner"] == "node"
        assert launch["entry_js"] == str(fake_npm)

    def test_install_creates_install_record(self, db_session, fake_npm):
        """验证安装后创建 MCPInstallRecord 记录（真实沙箱路径，非 /tmp 假路径）。"""
        install_sandbox(
            db=db_session,
            candidate_name="test2",
            package_ref="npm:test2",
            version="2.0",
            transport="stdio",
            declared_tools=["t1"],
            definition={},
        )
        db_session.flush()

        records = db_session.query(MCPInstallRecord).all()
        assert len(records) == 1
        assert records[0].server_name == "test2"
        assert "/tmp/aiive_sandbox" not in (records[0].sandbox_path or "")

    def test_install_creates_version_record(self, db_session, fake_npm):
        """验证安装后创建 CapabilityVersion 版本记录。"""
        install_sandbox(
            db=db_session,
            candidate_name="test3",
            package_ref="npm:test3",
            version="1.0",
            transport="stdio",
            declared_tools=["echo"],
            definition={},
        )
        db_session.flush()

        versions = db_session.query(CapabilityVersion).all()
        assert len(versions) == 1
        assert versions[0].tool_list_hash is not None

    def test_reinstall_with_same_hash_no_state_change(self, db_session, fake_npm):
        """验证相同哈希重新安装不改变状态。"""
        install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:same").first()
        assert cap.state == "sandbox"

    def test_hash_change_triggers_needs_review(self, db_session, fake_npm):
        """验证哈希变更时触发 needs_review 状态。"""
        install_sandbox(db_session, "changed", "npm:changed", "1.0", "stdio", ["echo"], {"v": 1})
        db_session.flush()
        install_sandbox(db_session, "changed", "npm:changed", "2.0", "stdio", ["echo"], {"v": 2})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:changed").first()
        assert cap.state == "needs_review"

    def test_rejects_package_not_in_whitelist(self, db_session, monkeypatch):
        """非白名单包必须被拒绝，且不触发 npm 执行。"""
        def _must_not_run(*args, **kwargs):
            raise AssertionError("非白名单包不应触发 npm install")

        monkeypatch.setattr(installer_mod, "_run_npm_install", _must_not_run)
        result = install_sandbox(
            db_session, "evil", "npm:evil-package", "1.0", "stdio", [], {},
        )
        assert result["ok"] is False
        assert result["installed"] is False
        assert "whitelist" in result["error"]
        assert db_session.query(Capability).count() == 0

    def test_npm_failure_is_honest_and_writes_nothing(self, db_session, monkeypatch):
        """npm 失败时如实返回错误，不留下半截 DB 记录。"""
        monkeypatch.setattr(
            discovery_mod, "allowed_npm_packages", lambda: _TEST_PACKAGES,
        )
        monkeypatch.setattr(
            installer_mod, "_run_npm_install",
            lambda p, v, d: {"ok": False, "entry_js": None,
                             "error": "npm install exit=1: boom",
                             "sandbox_path": str(d)},
        )
        result = install_sandbox(
            db_session, "test-server", "npm:test-server", "1.0", "stdio", [], {},
        )
        assert result["ok"] is False
        assert "npm install exit=1" in result["error"]
        assert db_session.query(Capability).count() == 0


class TestSmoke:
    """测试冒烟测试的激活和失败处理。"""

    def test_smoke_passed_activates_capability(self, db_session, fake_npm):
        """验证冒烟测试通过后能力状态变为 active。"""
        install_sandbox(db_session, "smoke-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:smoke-test", {"ok": True, "tool": "echo"})
        db_session.flush()
        assert result["state"] == "active"

    def test_smoke_failed_sets_needs_review(self, db_session, fake_npm):
        """验证冒烟测试失败后状态变为 needs_review。"""
        install_sandbox(db_session, "fail-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:fail-test", {"ok": False})
        db_session.flush()
        assert result["state"] == "needs_review"

    def test_smoke_retry_from_needs_review(self, db_session, fake_npm):
        """needs_review 状态允许再次冒烟（修复单向死路）。"""
        install_sandbox(db_session, "retry-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        run_smoke(db_session, "mcp:retry-test", {"ok": False, "error": "spawn failed"})
        db_session.flush()

        result = run_smoke(db_session, "mcp:retry-test", {"ok": True})
        assert result["state"] == "active"

    def test_smoke_unknown_capability(self, db_session):
        """验证对未知能力执行冒烟测试返回失败。"""
        result = run_smoke(db_session, "mcp:unknown", {"ok": True})
        assert result["ok"] is False

    def test_smoke_wrong_state(self, db_session, fake_npm):
        """验证对已激活能力重复冒烟测试返回失败。"""
        install_sandbox(db_session, "active-one", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        run_smoke(db_session, "mcp:active-one", {"ok": True})
        db_session.flush()

        # 已激活，不能再次冒烟
        result = run_smoke(db_session, "mcp:active-one", {"ok": True})
        assert result["ok"] is False


class _StubRuntime:
    """替代真实 MCP 运行时的 stub：可配置真实工具列表与调用结果。"""

    def __init__(self, tools: list[str], call_ok: bool = True):
        self._tools = [
            {"name": t, "description": "", "input_schema": {"type": "object", "properties": {}}}
            for t in tools
        ]
        self._call_ok = call_ok

    def configure(self, capability_id, spec):
        pass

    def list_tools(self, capability_id, timeout=60):
        return list(self._tools)

    def call_tool(self, capability_id, tool_name, arguments=None, timeout=60):
        if self._call_ok:
            return MCPToolResult(ok=True, result={"content": ["ok"]}, untrusted=True)
        return MCPToolResult(ok=False, error="tool call failed", untrusted=True)


@pytest.fixture
def stub_runtime_routes(monkeypatch):
    """把 smoke 路由的运行时替换为 stub（返回工厂便于各用例定制）。"""
    import aiive.api.routes_mcp_install as routes_mod

    def _install(tools: list[str], call_ok: bool = True) -> _StubRuntime:
        stub = _StubRuntime(tools, call_ok)
        monkeypatch.setattr(routes_mod, "get_runtime_client", lambda: stub)
        monkeypatch.setattr(routes_mod, "build_launch_spec", lambda launch: object())
        return stub

    return _install


class TestSmokeCapabilityDeclaredTool:
    """冒烟被测工具必须属于 server 的真实工具列表。

    旧版本无真实执行器，只能校验声明列表；现在冒烟真实执行 tools/list，
    校验对象升级为 server 实际暴露的工具。
    """

    def test_smoke_rejects_tool_not_in_real_list(
        self, db_session, fake_npm, stub_runtime_routes,
    ):
        """指定的工具不在真实工具列表 → 冒烟失败并写 needs_review。"""
        install_sandbox(db_session, "srv", "npm:srv", "1.0", "stdio", ["real_tool"], {})
        db_session.flush()
        stub_runtime_routes(["real_tool"])

        result = smoke_capability(
            "srv",
            SmokeRequest(tool_name="not_declared", params={}),
            db_session,
        )
        assert result["ok"] is True  # 状态机记录成功
        assert result["smoke_passed"] is False
        assert result["state"] == "needs_review"
        assert "not_declared" in result["smoke_result"]["error"]
        assert "real_tool" in result["smoke_result"]["error"]

    def test_smoke_real_tool_call_promotes_to_active(
        self, db_session, fake_npm, stub_runtime_routes,
    ):
        """真实工具列表内的工具调用成功 → 能力转 active。"""
        install_sandbox(db_session, "srv2", "npm:srv2", "1.0", "stdio", ["real_tool"], {})
        db_session.flush()
        stub_runtime_routes(["real_tool"], call_ok=True)

        result = smoke_capability(
            "srv2",
            SmokeRequest(tool_name="real_tool", params={}),
            db_session,
        )
        assert result["ok"] is True
        assert result["smoke_passed"] is True
        assert result["state"] == "active"

    def test_smoke_default_tools_list_mode(
        self, db_session, fake_npm, stub_runtime_routes,
    ):
        """不指定 tool_name 时默认做 tools/list 校验，并记录声明差异。"""
        install_sandbox(db_session, "srv2", "npm:srv2", "1.0", "stdio",
                        ["real_tool", "ghost_tool"], {})
        db_session.flush()
        stub_runtime_routes(["real_tool", "extra_tool"])

        result = smoke_capability("srv2", SmokeRequest(), db_session)
        assert result["smoke_passed"] is True
        assert result["state"] == "active"
        smoke = result["smoke_result"]
        assert smoke["mode"] == "tools_list"
        assert smoke["declared_missing"] == ["ghost_tool"]
        assert smoke["undeclared_extra"] == ["extra_tool"]

    def test_smoke_without_real_install_fails_honestly(self, db_session):
        """没有真实安装（无 launch 配置）时冒烟必须诚实失败。"""
        cap = Capability(
            capability_id="mcp:no-launch",
            name="no-launch",
            state="sandbox",
            definition={"name": "no-launch"},
        )
        db_session.add(cap)
        db_session.flush()

        result = smoke_capability("no-launch", SmokeRequest(), db_session)
        assert result["smoke_passed"] is False
        assert result["state"] == "needs_review"
        assert "launch" in result["smoke_result"]["error"]
