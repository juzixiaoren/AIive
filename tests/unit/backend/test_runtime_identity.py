from types import SimpleNamespace

from aiive import project_paths
from aiive.prompts import get_prompt_registry
from aiive.supervisor.slot_manager import SlotManager
from aiive.task_runtime.tools import build_main_agent_registry
from aiive.tools.registry import get_tool_registry


def test_identity_reports_loaded_slot_independently_of_active_pointer(tmp_path, monkeypatch):
    source = tmp_path / "source"
    slots = tmp_path / "slots"
    app = slots / "A" / "app"
    package = app / "backend" / "aiive"
    monkeypatch.setenv("AIIVE_SOURCE_ROOT", str(source))
    monkeypatch.setenv("AIIVE_SLOTS_ROOT", str(slots))
    monkeypatch.setenv("AIIVE_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("AIIVE_MANAGED_BY_SUPERVISOR", "1")
    monkeypatch.setenv("AIIVE_SLOT", "A")
    monkeypatch.setenv("AIIVE_LOADED_SLOT_VERSION", "0.1.4")
    monkeypatch.setenv("AIIVE_LOADED_SLOT_REVISION", "candidate-revision")
    monkeypatch.setenv("PRIVATE_TEST_KEY", "DO_NOT_RETURN")
    module_paths = {
        "aiive": package / "__init__.py",
        "aiive.runtime.system_contract": package / "runtime" / "system_contract.py",
    }
    monkeypatch.setattr(project_paths, "find_spec", lambda name: SimpleNamespace(origin=str(module_paths[name])))

    manager = SlotManager()
    manager.set_active_slot("A")
    main = build_main_agent_registry()
    worker = get_tool_registry()
    assert type(main.get("get_runtime_identity")) is type(worker.get("get_runtime_identity"))
    result = main.execute("get_runtime_identity", {}, "trusted_user_command")["result"]
    assert result["deployment_state"] == "loaded_active_slot"
    assert result["loaded_slot_matches_active"] is True
    assert result["runtime_app_root"] == str(app)
    assert result["canonical_source_root"] == str(source)
    assert result["loaded_slot_version"] == "0.1.4"
    assert result["loaded_slot_revision"] == "candidate-revision"
    prompts = get_prompt_registry()
    assert result["stable_contract_resource"] == prompts.resource_path("agent.stable_contract")
    assert result["stable_contract_audit_ref"] == prompts.render("agent.stable_contract").audit_ref
    assert "DO_NOT_RETURN" not in str(result)
    assert prompts.render("agent.stable_contract").content not in str(result)

    manager.set_active_slot("B")
    result = worker.execute("get_runtime_identity", {}, "trusted_user_command")["result"]
    assert result["loaded_slot"] == "A" and result["active_slot_pointer"] == "B"
    assert result["deployment_state"] == "restart_required"
    assert result["loaded_slot_matches_active"] is False
