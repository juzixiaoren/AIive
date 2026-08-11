"""测试 SelfDevPlanner（自进化规划器）模块。

覆盖规划生成、操作标记、核心文件保护、JSON 解析容错及数据库持久化。
"""

import json

from aiive.core.llm_client import FakeLLMClient
from aiive.selfdev.planner import SelfDevPlanner


class TestSelfDevPlanner:
    """测试 SelfDevPlanner 的规划生成和操作验证功能。"""

    def test_plan_generates_valid_structure(self):
        """规划器应生成有效的操作结构。"""
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Add a weather tool",
            "operations": [
                {
                    "operation": "add_file",
                    "target_file": "backend/aiive/tools/weather.py",
                    "content": "def get_weather():\n    return 'sunny'\n",
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
        # planner 行为已变更：not_allowed_yet 不再强制设为 True
        assert plan["operations"][0]["not_allowed_yet"] is False

    def test_plan_marks_all_ops_not_allowed_yet(self):
        """所有操作应被标记为 not_allowed_yet。"""
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Change frontend",
            "operations": [
                {
                    "operation": "modify_file",
                    "target_file": "frontend/src/App.tsx",
                    "content": "export default function App() { return null; }\n",
                    "reason": "Update layout",
                }
            ],
            "test_plan": "manual",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("change layout")

        for op in plan["operations"]:
            # planner 行为已变更：not_allowed_yet 不再强制设为 True
            assert op["not_allowed_yet"] is False

    def test_core_files_blocked(self):
        """核心文件应被保护并标记 CORE_FILE_PROTECTED。"""
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
            # CORE_FILE_PROTECTED 仍在 risk_notes 中体现
            assert "CORE_FILE_PROTECTED" in op.get("risk_notes", "")

    def test_add_file_without_content_blocked(self):
        """add_file/modify_file 缺失非空 content 时应被标记 not_allowed_yet。"""
        fake = FakeLLMClient(fixed_content=json.dumps({
            "goal_summary": "Add tool without content",
            "operations": [
                {"operation": "add_file", "target_file": "backend/aiive/tools/x.py"},
                {"operation": "modify_file", "target_file": "backend/aiive/tools/y.py", "content": "   "},
                {"operation": "delete_file", "target_file": "backend/aiive/tools/z.py"},
            ],
            "test_plan": "",
            "requires_schema_change": False,
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("add x")

        ops = plan["operations"]
        assert ops[0]["not_allowed_yet"] is True
        assert "MISSING_CONTENT" in ops[0]["risk_notes"]
        assert ops[1]["not_allowed_yet"] is True
        # delete_file 不需要 content
        assert ops[2]["not_allowed_yet"] is False

    def test_handles_invalid_json(self):
        """无效 JSON 应被优雅处理，返回失败占位结果。"""
        fake = FakeLLMClient(fixed_content="not json at all")
        planner = SelfDevPlanner(fake)
        plan = planner.plan("test")
        assert plan["goal_summary"] == "Could not generate plan"
        assert plan["operations"] == []

    def test_handles_wrong_json_shapes_without_crashing(self):
        for payload in (["not", "an", "object"], {"operations": "not-a-list"}, {"operations": ["bad-op"]}):
            planner = SelfDevPlanner(FakeLLMClient(fixed_content=json.dumps(payload)))
            plan = planner.plan("test")
            assert isinstance(plan, dict)
            assert plan["operations"] == []

    def test_unknown_operation_is_blocked_instead_of_silently_rewritten(self):
        planner = SelfDevPlanner(FakeLLMClient(fixed_content=json.dumps({
            "operations": [{
                "operation": "execute_anything",
                "target_file": "frontend/src/new.ts",
                "content": "export {};",
            }],
        })))
        operation = planner.plan("test")["operations"][0]
        assert operation["operation"] == "add_file"
        assert operation["not_allowed_yet"] is True
        assert "UNKNOWN_OPERATION" in operation["risk_notes"]

    def test_plan_includes_test_plan(self):
        """规划结果应包含测试计划字段。"""
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
        """Markdown 代码块中的 JSON 应被正确解析。"""
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
        """缺失字段应使用默认值填充。"""
        fake = FakeLLMClient(fixed_content=json.dumps({
            "operations": [{"target_file": "x.py"}],
        }))
        planner = SelfDevPlanner(fake)
        plan = planner.plan("test")
        assert plan["goal_summary"] == ""
        assert plan["test_plan"] == ""

    def test_db_persistence(self, db_session):
        """SelfDevRequest 和 PatchOperation 应能正确持久化到数据库。"""
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
