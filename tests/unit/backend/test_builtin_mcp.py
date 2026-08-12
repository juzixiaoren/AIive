"""零下载 AIive Essentials MCP 的登记和 ToolRegistry 恢复测试。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from aiive.db.models import CapabilityVersion, MCPInstallRecord
from aiive.mcp.bootstrap import activate_capability_tools
from aiive.mcp.builtin import BUILTIN_MCP_ID, ensure_builtin_mcp_capability
from aiive.mcp.runtime_client import build_launch_spec
from aiive.tools.registry import ToolRegistry


def test_builtin_mcp_seed_is_idempotent_and_active(db_session) -> None:
    first = ensure_builtin_mcp_capability(db_session)
    second = ensure_builtin_mcp_capability(db_session)
    assert first.id == second.id
    assert second.capability_id == BUILTIN_MCP_ID
    assert second.state == "active"
    assert db_session.query(MCPInstallRecord).filter(
        MCPInstallRecord.capability_id == second.id,
    ).count() == 1
    assert db_session.query(CapabilityVersion).filter(
        CapabilityVersion.capability_id == second.id,
    ).count() == 1


def test_builtin_mcp_uses_only_fixed_python_entry_and_registers_tools(db_session) -> None:
    capability = ensure_builtin_mcp_capability(db_session)
    spec = build_launch_spec(capability.definition["launch"])
    assert spec.command == sys.executable
    assert spec.args[0].endswith("aiive/mcp/builtin_server.py")
    assert "AIIVE_WEB_SEARCH_PROVIDER" in spec.optional_env_keys
    assert spec.env_keys == ()

    registry = ToolRegistry()
    result = activate_capability_tools(registry, capability)
    assert result["ok"] is True
    assert set(result["registered"]) == {
        "mcp_aiive_essentials_extract_document_text",
        "mcp_aiive_essentials_web_search",
        "mcp_aiive_essentials_web_fetch",
    }
    web_search = registry.get("mcp_aiive_essentials_web_search")
    assert web_search is not None
    assert web_search.safety.uses_network is True


def test_generic_search_tool_is_not_mistaken_for_network_access() -> None:
    registry = ToolRegistry()
    result = activate_capability_tools(
        registry,
        SimpleNamespace(
            capability_id="mcp:memory",
            name="memory",
            definition={
                "launch": {
                    "runner": "python_builtin",
                    "entry_py": str(
                        Path(__file__).resolve().parents[3]
                        / "backend/aiive/mcp/builtin_server.py"
                    ),
                    "args": [],
                    "env_keys": [],
                },
                "tools": [{
                    "name": "search_nodes",
                    "description": "Search a local memory graph.",
                    "input_schema": {"type": "object", "properties": {}},
                }],
                "risk_verdict": "low",
                "trust_level": "trusted",
            },
        ),
    )
    assert result["ok"] is True
    memory_search = registry.get("mcp_memory_search_nodes")
    assert memory_search is not None
    assert memory_search.safety.uses_network is False
