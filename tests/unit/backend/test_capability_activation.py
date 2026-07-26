"""能力激活路由的行为测试（真实 MCP 链路版本）。

旧语义（无真实 runtime 时激活恒返 ok=False 的 stub）已被真实链路取代。
新语义：
- 安装 + 冒烟通过的能力：激活返回 ok=True，真实工具注册进 ToolRegistry，
  plan.status=activated。
- 任一环节失败：ok=False，plan.status 回到 evaluated 可重试（不再进入
  不可恢复的 needs_user_review 死路），且不伪造 Capability。
"""
import pytest

from aiive.api.routes_capabilities import activate_capability
from aiive.db.models import Capability, CapabilityPlan
from aiive.mcp.runtime_client import MCPToolResult

_SERVER = "@modelcontextprotocol/server-memory"  # 目录内真实存在的候选


def _make_plan(db_session, selected: str = _SERVER, status: str = "evaluated"):
    plan = CapabilityPlan(
        goal="增加外部能力",
        selected_candidate=selected,
        status=status,
        risk_scores=[{"candidate": selected, "verdict": "low", "overall": 0.3}],
    )
    db_session.add(plan)
    db_session.flush()
    return plan


class _StubRuntime:
    def configure(self, capability_id, spec):
        pass

    def call_tool(self, capability_id, tool_name, arguments=None, timeout=60):
        return MCPToolResult(ok=True, result={"content": ["ok"]}, untrusted=True)


def test_activate_success_registers_tools(db_session, monkeypatch):
    """安装+冒烟已通过（state=active）的能力：激活成功并注册进 registry。"""
    from aiive.mcp import bootstrap
    from aiive.tools import registry as registry_mod
    from aiive.tools.registry import ToolRegistry

    cap = Capability(
        capability_id=f"mcp:{_SERVER}",
        name=_SERVER,
        state="active",
        definition={
            "name": _SERVER,
            "trust_level": "untrusted",
            "launch": {"runner": "node", "entry_js": "stubbed.js"},
            "tools": [{
                "name": "read_graph", "description": "read the graph",
                "input_schema": {"type": "object", "properties": {}},
            }],
        },
    )
    db_session.add(cap)
    plan = _make_plan(db_session)

    fresh_registry = ToolRegistry()
    monkeypatch.setattr(registry_mod, "get_tool_registry", lambda: fresh_registry)
    monkeypatch.setattr(bootstrap, "get_runtime_client", lambda: _StubRuntime())
    monkeypatch.setattr(bootstrap, "build_launch_spec", lambda launch: object())

    result = activate_capability(plan.id, db_session)

    assert result.ok is True
    assert result.capability_id == f"mcp:{_SERVER}"
    assert "mcp_server_memory_read_graph" in result.registered_tools
    assert plan.status == "activated"
    assert plan.capability_id == f"mcp:{_SERVER}"

    reg = fresh_registry.get("mcp_server_memory_read_graph")
    assert reg is not None
    assert reg.safety.definition_source == "remote_mcp"


def test_activate_smoke_failure_keeps_plan_retryable(db_session):
    """冒烟失败：ok=False，plan.status 回到 evaluated（可重试），能力 needs_review。

    launch 指向不存在的入口 js，真实 build_launch_spec 会拒绝 → 冒烟诚实失败。
    """
    cap = Capability(
        capability_id=f"mcp:{_SERVER}",
        name=_SERVER,
        state="sandbox",
        definition={
            "name": _SERVER,
            "launch": {"runner": "node", "entry_js": "Z:/no/such/entry.js"},
        },
    )
    db_session.add(cap)
    plan = _make_plan(db_session)

    result = activate_capability(plan.id, db_session)

    assert result.ok is False
    assert "smoke failed" in result.error
    assert plan.status == "evaluated"  # 可重试，不是单向死路
    assert cap.state == "needs_review"


def test_activate_unknown_candidate_fails_closed(db_session):
    """选中候选不在目录中：不得创建或激活任何 Capability。"""
    plan = _make_plan(db_session, selected="example-server")

    result = activate_capability(plan.id, db_session)

    assert result.ok is False
    assert "catalog" in result.error
    assert plan.status == "evaluated"
    assert db_session.query(Capability).count() == 0


def test_activate_rejects_terminal_plan_status(db_session):
    """已 activated 的计划不能重复激活。"""
    plan = _make_plan(db_session, status="activated")
    result = activate_capability(plan.id, db_session)
    assert result.ok is False
    assert "status=activated" in result.error


def test_activate_legacy_needs_user_review_is_recoverable(db_session):
    """历史死路数据（needs_user_review）允许重新进入激活流程。"""
    plan = _make_plan(db_session, selected="example-server", status="needs_user_review")

    result = activate_capability(plan.id, db_session)

    # 候选不在目录 → 激活失败，但计划被拉回 evaluated 可重试
    assert result.ok is False
    assert plan.status == "evaluated"
