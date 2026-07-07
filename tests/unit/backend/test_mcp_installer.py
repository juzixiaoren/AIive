from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord
from aiive.mcp.installer import install_sandbox, run_smoke


class TestMCPInstaller:
    def test_install_sandbox_creates_capability(self, db_session):
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
        r1 = install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        r2 = install_sandbox(db_session, "same", "npm:same", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:same").first()
        assert cap.state == "sandbox"

    def test_hash_change_triggers_needs_review(self, db_session):
        install_sandbox(db_session, "changed", "npm:changed", "1.0", "stdio", ["echo"], {"v": 1})
        db_session.flush()
        install_sandbox(db_session, "changed", "npm:changed", "2.0", "stdio", ["echo"], {"v": 2})
        db_session.flush()

        cap = db_session.query(Capability).filter(Capability.capability_id == "mcp:changed").first()
        assert cap.state == "needs_review"


class TestSmoke:
    def test_smoke_passed_activates_capability(self, db_session):
        install_sandbox(db_session, "smoke-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:smoke-test", {"ok": True, "tool": "echo"})
        db_session.flush()
        assert result["state"] == "active"

    def test_smoke_failed_sets_needs_review(self, db_session):
        install_sandbox(db_session, "fail-test", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()

        result = run_smoke(db_session, "mcp:fail-test", {"ok": False})
        db_session.flush()
        assert result["state"] == "needs_review"

    def test_smoke_unknown_capability(self, db_session):
        result = run_smoke(db_session, "mcp:unknown", {"ok": True})
        assert result["ok"] is False

    def test_smoke_wrong_state(self, db_session):
        install_sandbox(db_session, "active-one", "npm:test", "1.0", "stdio", ["echo"], {})
        db_session.flush()
        run_smoke(db_session, "mcp:active-one", {"ok": True})
        db_session.flush()

        # Already active, can't smoke again
        result = run_smoke(db_session, "mcp:active-one", {"ok": True})
        assert result["ok"] is False
