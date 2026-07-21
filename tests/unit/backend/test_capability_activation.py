"""能力激活路由的 fail-closed 回归测试。"""

from aiive.api.routes_capabilities import activate_capability
from aiive.db.models import Capability, CapabilityPlan


def test_activate_requires_real_mcp_runtime(db_session):
    """没有真实 MCP runtime 和 smoke 时不得创建或激活 Capability。"""
    plan = CapabilityPlan(
        goal="增加外部能力",
        selected_candidate="example-server",
        status="evaluated",
    )
    db_session.add(plan)
    db_session.flush()

    result = activate_capability(plan.id, db_session)

    assert result.ok is False
    assert result.error == "Real MCP runtime and smoke verification are required before activation"
    assert result.smoke_result is not None
    assert result.smoke_result["code"] == "real_mcp_runtime_unavailable"
    assert plan.status == "needs_user_review"
    assert db_session.query(Capability).count() == 0
