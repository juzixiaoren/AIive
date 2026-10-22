"""工具注册、发现与分层注入契约测试（离线，无外部 API）。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from aiive.control.capability_broker import CapabilityBroker
from aiive.control.scope import TaskScope
from aiive.db.models import AgentAction, Thread
from aiive.task_runtime.agent_executor import AgentDecision
from aiive.task_runtime.context_assembler import TaskContextAssembler
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.router import normalize_task_brief
from aiive.task_runtime.runtime import PersistentTaskRuntime
from aiive.task_runtime.schemas import ActionProposal, TaskStatus
from aiive.task_runtime.tool_injection import (
    build_tool_view,
    record_definition,
    record_search,
    resident_definitions,
)
from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    get_tool_registry,
)


class _ScriptedAgent:
    """按脚本返回决策，并记录每轮收到的常驻能力列表。"""

    def __init__(self, *proposals: ActionProposal) -> None:
        self.proposals = list(proposals)
        self.capabilities_seen: list[list[dict]] = []

    def decide(self, task, capabilities):
        self.capabilities_seen.append(list(capabilities))
        proposal = self.proposals.pop(0)
        return AgentDecision(
            proposal=proposal,
            model="fake-task-model",
            token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            input_snapshot={"task_id": task.id},
            prompt_refs=["task_runtime.worker_system@test"],
        )


def _task(db_session, scope: dict, goal: str = "发现并使用工具"):
    thread = Thread(title="tool injection")
    db_session.add(thread)
    db_session.flush()
    return TaskRepository(db_session).create_task(
        thread_id=thread.id,
        goal=goal,
        task_brief={"goal": goal, "scope": scope},
        enqueue=False,
    )


def _probe_registration(description: str = "probe tool") -> ToolRegistration:
    return ToolRegistration(
        safety=CapabilitySafetySchema(
            capability_id="probe_tool",
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="low",
        ),
        handler=lambda **_: {"ok": True},
        description=description,
        parameters={"query": {"type": "str", "required": True}},
    )


def test_catalog_is_lightweight_and_definition_is_complete() -> None:
    """目录只给选择所需字段；完整 Schema 必须显式查询。"""
    registry = get_tool_registry()
    catalog = registry.catalog()
    assert catalog
    assert all("parameters" not in item and "input_schema" not in item for item in catalog)
    definition = registry.definition("web_search")
    assert definition is not None
    assert "parameters" in definition and "input_schema" in definition
    assert definition["descriptor_hash"]
    assert registry.definition("not_a_capability") is None


def test_search_is_deterministic_and_respects_limit() -> None:
    """同一注册表状态下检索结果稳定、可复现。"""
    registry = get_tool_registry()
    first = registry.search("web", limit=3)
    second = registry.search("web", limit=3)
    assert first == second
    assert len(first) <= 3
    assert {"web_search", "fetch_web_page"} & {item["capability_id"] for item in first}
    assert registry.search("   ") == []
    assert registry.search("web", limit=0) == []


def test_unregister_removes_tool_from_catalog_and_search() -> None:
    """卸载后目录与检索立即一致，不存在陈旧索引。"""
    registry = ToolRegistry()
    registry.register(_probe_registration())
    assert [item["capability_id"] for item in registry.catalog()] == ["probe_tool"]
    assert registry.search("probe")
    assert registry.unregister("probe_tool") is True
    assert registry.catalog() == []
    assert registry.search("probe") == []
    assert registry.definition("probe_tool") is None
    assert registry.unregister("probe_tool") is False


def test_discovery_covers_tools_outside_restricted_scope(db_session) -> None:
    """发现范围不按任务能力白名单收窄，但结果带 in_scope 标记。"""
    task = _task(db_session, {"allowed_capabilities": ["get_current_time"]})
    broker = CapabilityBroker(db_session)
    results = broker.search_capabilities(task, "web", limit=5)
    web = next(item for item in results if item["capability_id"] == "web_search")
    assert web["in_scope"] is False
    definition = broker.describe_capability(task, "web_search")
    assert definition is not None
    assert definition["in_scope"] is False
    assert "input_schema" in definition
    assert broker.describe_capability(task, "not_a_capability") is None


def test_unified_policy_keeps_real_constraints() -> None:
    """统一策略放开能力白名单，但路径/网络/密钥约束保持不变。"""
    unified = TaskScope.from_brief({"scope": {"capability_policy": "unified"}})
    assert unified.allows_capability("safe_delete")
    assert unified.validate_arguments("safe_delete", {"path": "/tmp/x"}) == ["path_scope_missing:path"]
    restricted = TaskScope.from_brief({"scope": {"allowed_capabilities": ["echo"]}})
    assert restricted.allows_capability("echo")
    assert not restricted.allows_capability("safe_delete")
    assert restricted.validate_arguments("safe_delete", {"path": "/tmp/x"}) == [
        "capability_not_in_task_scope:safe_delete",
        "path_scope_missing:path",
    ]


def test_task_type_no_longer_narrows_discovery() -> None:
    """task_type 不再生成能力白名单；显式列表仍按 restricted 执行。"""
    unified = normalize_task_brief("research", {})
    assert unified["scope"]["allowed_capabilities"] == []
    assert unified["scope"]["capability_policy"] == "unified"
    restricted = normalize_task_brief("research", {"scope": {"allowed_capabilities": ["web_search"]}})
    assert restricted["scope"]["capability_policy"] == "restricted"


def test_loaded_definition_is_rebuilt_and_stale_hash_is_flagged() -> None:
    """定义变更或卸载后不注入旧 Schema，而是要求重新查询。"""
    registry = ToolRegistry()
    registry.register(_probe_registration())
    state = record_definition({}, "probe_tool", registry.definition("probe_tool")["descriptor_hash"])
    ready = build_tool_view(registry, [], state)
    assert ready["loaded"][0]["status"] == "ready"
    assert "parameters" in ready["loaded"][0]

    registry.register(_probe_registration("probe tool v2"))
    changed = build_tool_view(registry, [], state)
    assert changed["loaded"][0]["status"] == "definition_changed"
    assert "parameters" not in changed["loaded"][0]

    registry.unregister("probe_tool")
    missing = build_tool_view(registry, [], state)
    assert missing["loaded"][0]["status"] == "unavailable"


def test_discovered_ids_are_reconciled_against_current_catalog() -> None:
    """检索结果只存 id；目录变化后 removed 显式列出。"""
    registry = ToolRegistry()
    registry.register(_probe_registration())
    state = record_search({}, "probe", ["probe_tool", "gone_tool"])
    view = build_tool_view(registry, [], state)
    assert [item["capability_id"] for item in view["discovered"][0]["results"]] == ["probe_tool"]
    assert view["discovered"][0]["removed"] == ["gone_tool"]


def test_worker_prompt_uses_discovery_mode_and_hides_non_resident_tools(db_session) -> None:
    """大目录下只注入常驻能力，不出现其他工具的完整 Schema。"""
    task = _task(db_session, {"capability_policy": "unified"})
    broker = CapabilityBroker(db_session)
    registry = broker.registry_for(task)
    resident = resident_definitions(registry)
    messages, snapshot, _refs = TaskContextAssembler(db_session).messages(
        task, resident, registry=registry,
    )
    assert snapshot["tools"]["mode"] == "discovery"
    assert "capability_ids" not in snapshot
    resident_ids = {item["capability_id"] for item in snapshot["tools"]["resident"]}
    assert "memory_search" in resident_ids
    assert "search_capabilities" in messages[0]["content"]
    content = messages[1]["content"]
    assert "safe_delete" not in content
    # 只有常驻能力带完整 Schema；其余工具不注入参数定义。
    assert content.count('"input_schema"') == len(resident)


def test_small_catalog_injects_full_schemas(db_session) -> None:
    """目录较小时直接注入完整 Schema，避免多余模型调用。"""
    task = _task(db_session, {"allowed_capabilities": ["echo", "get_current_time"]})
    broker = CapabilityBroker(db_session)
    messages, snapshot, _refs = TaskContextAssembler(db_session).messages(
        task, [], registry=broker.registry_for(task),
    )
    assert snapshot["tools"]["mode"] == "full"
    available_ids = {item["capability_id"] for item in snapshot["tools"]["available"]}
    assert available_ids == {"echo", "get_current_time"}
    assert '"input_schema"' in messages[1]["content"]


def test_runtime_discovery_creates_no_action_and_injects_results(db_session) -> None:
    """发现决策只读：不产生 AgentAction，结果进入下一轮上下文。"""
    task = _task(db_session, {"allowed_capabilities": ["get_current_time"]})
    agent = _ScriptedAgent(
        ActionProposal(decision="search_capabilities", query="web"),
        ActionProposal(decision="describe_capability", capability_id="web_search"),
        ActionProposal(decision="action", capability_id="get_current_time"),
        ActionProposal(decision="finish", summary="done", report_status="succeeded"),
    )
    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    assert result["status"] == TaskStatus.SUCCEEDED.value
    actions = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).all()
    assert [item.capability_id for item in actions] == ["get_current_time"]

    db_session.refresh(task)
    discovery = task.task_state["tool_discovery"]
    assert discovery["queries"][0]["query"] == "web"
    assert "web_search" in discovery["queries"][0]["capability_ids"]
    assert discovery["loaded"][0]["capability_id"] == "web_search"
    assert int(task.usage["discovery_steps"]) == 2

    broker = CapabilityBroker(db_session)
    registry = broker.registry_for(task)
    _messages, snapshot, _refs = TaskContextAssembler(db_session).messages(
        task, resident_definitions(registry), registry=registry,
    )
    discovered = snapshot["tools"]["discovered"][0]
    assert "web_search" in [item["capability_id"] for item in discovered["results"]]
    assert snapshot["tools"]["loaded"][0]["status"] == "ready"


def test_action_proposal_requires_discovery_payload() -> None:
    """发现类决策必须携带检索词或能力 id，避免空转。"""
    with pytest.raises(ValidationError):
        ActionProposal(decision="search_capabilities", query="  ")
    with pytest.raises(ValidationError):
        ActionProposal(decision="describe_capability", capability_id="")
    assert ActionProposal(decision="search_capabilities", query="web").query == "web"


def test_execution_does_not_depend_on_injection(db_session) -> None:
    """统一策略下，未注入的能力仍可执行：权限由 Broker 判定，不由上下文决定。"""
    task = _task(db_session, {"capability_policy": "unified"})
    agent = _ScriptedAgent(
        ActionProposal(decision="action", capability_id="get_current_time"),
        ActionProposal(decision="finish", summary="done"),
    )
    result = PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    assert result["status"] == TaskStatus.SUCCEEDED.value
    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.capability_id == "get_current_time"
    assert action.status == "succeeded"


def test_restricted_scope_still_blocks_unlisted_capability(db_session) -> None:
    """显式授权白名单仍是真实约束：越权提案在执行前被拦截。"""
    task = _task(db_session, {"allowed_capabilities": ["echo"]})
    agent = _ScriptedAgent(
        ActionProposal(decision="action", capability_id="get_current_time"),
        ActionProposal(decision="finish", summary="blocked"),
    )
    PersistentTaskRuntime(db_session, agent_executor=agent).run(task.id)

    action = db_session.query(AgentAction).filter(AgentAction.task_id == task.id).one()
    assert action.status == "failed"
    assert action.error_message in {"unknown_capability", "capability_not_in_task_scope:get_current_time"}


def test_dynamically_registered_tool_is_discoverable_without_prompt_change(db_session) -> None:
    """MCP/自进化新增能力注册后自动进入目录，无需修改系统提示词。"""
    registry = get_tool_registry()
    registry.register(_probe_registration("mcp 桥接工具：读取远端资源"))
    try:
        task = _task(db_session, {"capability_policy": "unified"})
        broker = CapabilityBroker(db_session)
        hits = broker.search_capabilities(task, "probe")
        assert "probe_tool" in [item["capability_id"] for item in hits]
        definition = broker.describe_capability(task, "probe_tool")
        assert definition is not None
        assert definition["descriptor_hash"]
        assert definition["in_scope"] is True
    finally:
        registry.unregister("probe_tool")


def test_resident_entries_stay_visible_when_scope_is_restricted(db_session) -> None:
    """常驻入口始终可见；restricted 时用 in_scope=false 表达当前不可执行。"""
    task = _task(db_session, {"allowed_capabilities": ["get_current_time"]})
    broker = CapabilityBroker(db_session)
    registry = broker.registry_for(task)
    resident = resident_definitions(broker.available_registry(task))
    _messages, snapshot, _refs = TaskContextAssembler(db_session).messages(
        task, resident, registry=registry,
    )
    entries = {item["capability_id"]: item for item in snapshot["tools"]["resident"]}
    assert "memory_search" in entries
    assert entries["memory_search"]["in_scope"] is False
