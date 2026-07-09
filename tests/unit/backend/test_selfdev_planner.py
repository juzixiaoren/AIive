import json

from aiive.core.llm_client import FakeLLMClient
from aiive.selfdev.planner import SelfDevPlanner


class TestSelfDevPlanner:
    def test_plan_generates_valid_structure(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Add a weather tool",
            "operations": [
                {
                    "operation": "add_file",
                    "target_file": "backend/aiive/tools/weather.py",
                    "reason": "User wants weather capability",
                    "risk_notes": "External API call",
                    "requires_schema_change": False,
                    "safe_delete_scope": None,
                    "not_allowed_yet": False,
                }
            ],
            "test_plan": "pytest tests/unit/test_weather.py",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("add weather")

        assert plan["goal_summary"] == "Add a weather tool"
        assert len(plan["operations"]) == 1
        assert plan["operations"][0]["not_allowed_yet"] is True  # V12 marks all as not_allowed_yet

    def test_plan_marks_all_ops_not_allowed_yet(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Change frontend",
            "operations": [
                {
                    "operation": "modify_file",
                    "target_file": "frontend/src/App.tsx",
                    "reason": "Update layout",
                }
            ],
            "test_plan": "manual",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("change layout")

        for op in plan["operations"]:
            assert op["not_allowed_yet"] is True

    def test_core_files_blocked(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Modify main.py",
            "operations": [
                {
                    "operation": "modify_file",
                    "target_file": "backend/aiive/main.py",
                    "reason": "Add route",
                }
            ],
            "test_plan": "",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("modify main")

        for op in plan["operations"]:
            assert op["not_allowed_yet"] is True
            assert "CORE_FILE_PROTECTED" in op.get("risk_notes", "")

    def test_handles_invalid_json(self):
        fake = FakeLLMClient(fixed_content="not json at all")
        planner = SelfDevPlanner(fake)
        plan = planner.plan("test")
        assert plan["goal_summary"] == "Could not generate plan"
        assert plan["operations"] == []

    def test_plan_includes_test_plan(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Add feature",
            "operations": [],
            "test_plan": "Run pytest",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("add feature")
        assert plan["test_plan"] == "Run pytest"

    def test_handles_markdown_json_block(self):
        fake = FakeLLMClient(fixed_content="```json\n" + json.dumps({
            "goal_summary": "Add X",
            "operations": [],
            "test_plan": "",
            "requires_schema_change": False,
        }) + "\n```")
        planner = SelfDevPlanner(fake)
        plan = planner.plan("add X")
        assert plan["goal_summary"] == "Add X"

    def test_missing_fields_get_defaults(self):
        fake = FakeLLMClient(fixed_content=json.dumps({
            "operations": [{"target_file": "x.py"}],
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("test")
        assert plan["goal_summary"] == ""
        assert plan["test_plan"] == ""

    def test_db_persistence(self, db_session):
        from aiive.db.models import SelfDevRequest, PatchOperation

        req = SelfDevRequest(
            trace_id="trace-1",
            goal="Test goal",
            plan={"ops": []},
        )
        db_session.add(req)
        db_session.flush()

        op = PatchOperation(
            request_id=req.id,
            operation="add_file",
            target_file="test.py",
            reason="test",
            not_allowed_yet=True,
        )
        db_session.add(op)
        db_session.flush()

        fetched = db_session.get(SelfDevRequest, req.id)
        assert fetched.goal == "Test goal"

        ops = db_session.query(PatchOperation).filter(PatchOperation.request_id == req.id).all()
        assert len(ops) == 1
        assert ops[0].not_allowed_yet is True
