import json
from typing import Any

from aiive.core.llm_client import LLMClient, LLMResponse

PLAN_PROMPT = (
    "You are an AI system architect. Given a user requirement, produce a structured "
    "patch plan for the AIive personal agent project. DO NOT apply any changes. "
    "Only generate a plan.\n\n"
    "## Project Structure Available\n"
    "backend/aiive/ - FastAPI backend (api/, core/, db/, runtime/, memory/, tools/, mcp/, supervisor/, selfdev/)\n"
    "frontend/ - React + Vite + TypeScript\n"
    "tests/unit/backend/ - pytest unit tests\n"
    "docs/ - design docs\n\n"
    "## Rules\n"
    "- NEVER suggest modifying active slot or inactive slot in this phase\n"
    "- If an operation requires applying patches, mark it not_allowed_yet\n"
    "- If an operation involves deletion, declare safe_delete_scope\n"
    "- If schema changes are needed, mark requires_schema_change\n"
    "- External tool installs must go through MCP sandbox\n\n"
    "## Output Format (JSON only, no markdown)\n"
    "{{\n"
    '  "goal_summary": "one-line summary",\n'
    '  "operations": [\n'
    '    {{\n'
    '      "operation": "add_file | modify_file | delete_file",\n'
    '      "target_file": "path/to/file.py",\n'
    '      "reason": "why this change is needed",\n'
    '      "risk_notes": "potential risks if any",\n'
    '      "requires_schema_change": false,\n'
    '      "safe_delete_scope": "optional scope id",\n'
    '      "not_allowed_yet": false\n'
    "    }}\n"
    "  ],\n"
    '  "test_plan": "how to test the changes",\n'
    '  "requires_schema_change": false\n'
    "}}\n\n"
    "User Requirement: {goal}\n\n"
    "Output ONLY valid JSON:"
)

ALLOWED_OPERATIONS = {"add_file", "modify_file", "delete_file"}
FORBIDDEN_PATHS = {"main.py", "config.py", "db/base.py", "db/models.py"}


class SelfDevPlanner:
    def __init__(self, llm_client: LLMClient):
        self._llm_client = llm_client

    def plan(self, goal: str, trace_id: str | None = None) -> dict[str, Any]:
        prompt = PLAN_PROMPT.format(goal=goal)
        messages = [{"role": "user", "content": prompt}]

        response: LLMResponse = self._llm_client.chat(
            messages, trace_id=trace_id, temperature=0.2
        )
        return self._parse(response.content)

    def _parse(self, raw: str) -> dict[str, Any]:
        try:
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1])
            plan = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return self._empty_plan()

        return self._validate(plan)

    def _validate(self, plan: dict) -> dict[str, Any]:
        operations = plan.get("operations", [])

        for op in operations:
            op.setdefault("operation", "add_file")
            op.setdefault("not_allowed_yet", False)
            op.setdefault("requires_schema_change", False)
            op.setdefault("safe_delete_scope", None)
            op.setdefault("risk_notes", "")

            if op["operation"] not in ALLOWED_OPERATIONS:
                op["operation"] = "add_file"

            # Mark forbidden paths
            for forbidden in FORBIDDEN_PATHS:
                if forbidden in op.get("target_file", ""):
                    op["not_allowed_yet"] = True
                    op["risk_notes"] = (op.get("risk_notes", "") +
                        " CORE_FILE_PROTECTED: cannot modify core infrastructure in this phase.")

            # Mark all "apply" operations as not_allowed_yet (V12 policy)
            op["not_allowed_yet"] = True

        plan.setdefault("test_plan", "")
        plan.setdefault("goal_summary", plan.get("goal_summary", ""))
        plan.setdefault("requires_schema_change", any(
            op.get("requires_schema_change") for op in operations
        ))

        return plan

    def _empty_plan(self) -> dict[str, Any]:
        return {
            "goal_summary": "Could not generate plan",
            "operations": [],
            "test_plan": "",
            "requires_schema_change": False,
        }
