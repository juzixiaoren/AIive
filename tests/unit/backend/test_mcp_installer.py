"""测试 MCP 安装器——沙箱安装、版本管理和冒烟测试。"""
from aiive.api.routes_mcp_install import SmokeRequest, smoke_capability
from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord
from aiive.mcp.installer import install_sandbox, run_smoke


class TestMCPInstaller:
    """测试沙箱安装、重复安装和哈希变更检测。"""

    def test_install_sandbox_creates_capability(self, db_session):
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
        assert result["installed"] is True
        assert result["state"] == "sandbox"

        cap = (
            db_session.query(Capability)
            .filter(Capability.capability_id == "mcp:test-server")
            .first()
        )
        assert cap is not None
        assert cap.state == "sandbox"

    def test_install_creates_install_record(self, db_session):
        """验证安装后创建 MCPInstallRecord 记录。"""
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

    def test_install_creates_version_record(self, db_session):
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

    def test_reinstall_with_same_hash_no_state_change(self, db_session):
        """验证相同哈希重新安装不改变状态。"""
        r1 = install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        r2 = install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:same").first()
        assert cap.state == "sandbox"

    def test_hash_change_triggers_needs_review(self, db_session):
        """验证哈希变更时触发 needs_review 状态。"""
        install_sandbox(db_session, "changed", "npm:changed", "1.0", "stdio", ["echo"], {"v": 1})
        db_session.flush()
        install_sandbox(db_session, "changed", "npm:changed", "2.0", "stdio", ["echo"], {"v": 2})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:changed").first()
        assert cap.state == "needs_review"


class TestSmoke:
    """测试冒烟测试的激活和失败处理。"""

    def test_smoke_passed_activates_capability(self, db_session):
        """验证冒烟测试通过后能力状态变为 active。"""
        install_sandbox(db_session, "smoke-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:smoke-test", {"ok": True, "tool": "echo"})
        db_session.flush()
        assert result["state"] == "active"

    def test_smoke_failed_sets_needs_review(self, db_session):
        """验证冒烟测试失败后状态变为 needs_review。"""
        install_sandbox(db_session, "fail-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:fail-test", {"ok": False})
        db_session.flush()
        assert result["state"] == "needs_review"

    def test_smoke_unknown_capability(self, db_session):
        """验证对未知能力执行冒烟测试返回失败。"""
        result = run_smoke(db_session, "mcp:unknown", {"ok": True})
        assert result["ok"] is False

    def test_smoke_wrong_state(self, db_session):
        """验证对已激活能力重复冒烟测试返回失败。"""
        install_sandbox(db_session, "active-one", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        run_smoke(db_session, "mcp:active-one", {"ok": True})
        db_session.flush()

        # 已激活，不能再次冒烟
        result = run_smoke(db_session, "mcp:active-one", {"ok": True})
        assert result["ok"] is False


class TestSmokeCapabilityDeclaredTool:
    """回归测试：P1 问题 18。冒烟被测工具必须确属该能力声明。

    修复前冒烟注册 echo/list_files 等无关 stub 却调用真实工具名，
    造成“逻辑矛盾”。现在校验被测工具确属该能力声明，否则明确失败。
    """

    def test_smoke_rejects_undeclared_tool(self, db_session):
        """冒烟未声明工具应明确失败，而非用无关 stub 冒充。"""
        install_sandbox(db_session, "srv", "npm:srv", "1.0", "stdio", ["real_tool"], {})
        db_session.flush()

        result = smoke_capability(
            "srv",
            SmokeRequest(tool_name="not_declared", params={}),
            db_session,
        )
        assert result["ok"] is False
        assert "not_declared" in result["error"]
        assert "real_tool" in result["error"]

    def test_smoke_accepts_declared_tool(self, db_session):
        """冒烟已声明工具应通过校验（无真实执行器时诚实置 needs_review）。"""
        install_sandbox(db_session, "srv2", "npm:srv2", "1.0", "stdio", ["real_tool"], {})
        db_session.flush()

        result = smoke_capability(
            "srv2",
            SmokeRequest(tool_name="real_tool", params={}),
            db_session,
        )
        # 通过校验后进入执行：当前无真实 MCP 执行器，应诚实失败而非伪造成功
        assert result["ok"] is True
        assert result["state"] == "needs_review"
