"""MCP 真实运行链路的单元测试（不依赖网络 / npm）。

覆盖：
- discovery 按匹配得分降序排序、npm 包白名单
- planner 的 recommended 可达性、analyze_goal 关键词驱动搜索
- installer 的包名白名单拒绝、DB 记账（monkeypatch 掉真实 npm）
- run_smoke 状态机（含 needs_review 可重试）
- runtime_client 同步封装（Python 假 MCP stdio server 作夹具，不依赖 node）
- bootstrap 把 MCP 工具注册进 ToolRegistry（stub 运行时）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from aiive.mcp import installer as installer_mod
from aiive.mcp.capability_planner import CapabilityPlanner
from aiive.mcp.discovery import (
    allowed_npm_packages,
    get_candidate_by_name,
    search_mcp_candidates,
)
from aiive.mcp.installer import install_sandbox, run_smoke

_FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_results_sorted_by_score_desc(self):
        results = search_mcp_candidates("filesystem file read write search")
        assert results, "应至少匹配到一个候选"
        scores = [c.match_score for c in results]
        assert scores == sorted(scores, reverse=True)
        # filesystem server 应因名称+描述+工具名多重命中排第一
        assert results[0].name == "@modelcontextprotocol/server-filesystem"

    def test_best_match_first_for_sequential_thinking(self):
        results = search_mcp_candidates("sequential thinking reasoning")
        assert results[0].name == "@modelcontextprotocol/server-sequential-thinking"

    def test_empty_goal_returns_full_catalog(self):
        results = search_mcp_candidates("")
        assert len(results) == 4

    def test_whitelist_contains_catalog_packages(self):
        packages = allowed_npm_packages()
        assert "@modelcontextprotocol/server-filesystem" in packages
        assert "evil-package" not in packages

    def test_get_candidate_by_name(self):
        c = get_candidate_by_name("@modelcontextprotocol/server-memory")
        assert c is not None
        assert c.definition_trust_level == "semi_trusted"
        assert c.required_env == []
        assert get_candidate_by_name("no-such-server") is None


# ---------------------------------------------------------------------------
# planner
# ---------------------------------------------------------------------------

class _StubLLM:
    """返回固定 JSON 的 LLM stub，同时记录调用次数。"""

    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1

        class _Resp:
            content = self._content

        return _Resp()


class TestPlanner:
    def test_recommended_reachable_for_official_low_permission(self):
        planner = CapabilityPlanner(llm_client=None)  # evaluate 不用 LLM
        candidate = {
            "name": "official-low-risk",
            "source": "official_registry",
            "version": "1.0.0",
            "declared_tools": ["read_a", "read_b"],
            "risk_notes": "read only access",
            "definition_trust_level": "semi_trusted",
        }
        evals = planner.evaluate_candidates([candidate])
        assert evals[0].risk_score.verdict == "low"
        assert evals[0].recommendation == "recommended"

    def test_untrusted_candidate_still_needs_review(self):
        planner = CapabilityPlanner(llm_client=None)
        candidate = {
            "name": "community-risky",
            "source": "community",
            "version": "0.0.1",
            "declared_tools": ["delete_everything", "write_stuff", "a", "b", "c", "d"],
            "risk_notes": "can delete and write anything",
            "definition_trust_level": "untrusted",
        }
        evals = planner.evaluate_candidates([candidate])
        assert evals[0].recommendation == "needs_review"

    def test_real_catalog_official_candidate_reaches_recommended(self):
        planner = CapabilityPlanner(llm_client=None)
        raw = planner.search_candidates(["filesystem"])
        evals = planner.evaluate_candidates(raw)
        assert any(e.recommendation == "recommended" for e in evals), (
            "官方源低权限候选必须能达到 recommended"
        )

    def test_plan_from_goal_uses_llm_keywords_for_search(self):
        llm = _StubLLM(
            '{"goal_summary": "需要读文件", "missing_capability_type": "filesystem",'
            ' "search_keywords": ["filesystem"], "risk_tolerance": "low",'
            ' "reasoning": "test"}'
        )
        planner = CapabilityPlanner(llm)
        # 目标文本本身不含任何目录关键词，只有 LLM 关键词能搜到候选
        plan = planner.plan_from_goal("帮我看看那个东西")
        assert llm.calls == 1, "analyze_goal 只应调用一次 LLM"
        assert plan.candidates, "应通过 LLM 关键词搜到候选"
        assert plan.selected == "@modelcontextprotocol/server-filesystem"
        assert plan.status == "evaluated"


# ---------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------

class TestInstaller:
    def test_rejects_non_exact_version(self, db, monkeypatch):
        def _fail_if_called(*args, **kwargs):
            raise AssertionError("非法版本不应触发 npm install")

        monkeypatch.setattr(installer_mod, "_run_npm_install", _fail_if_called)
        result = install_sandbox(
            db,
            "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory",
            "npm:evil-package@1.0.0",
            "stdio",
            [],
            {"name": "memory"},
        )
        assert result["ok"] is False
        assert "exact semantic version" in result["error"]

    def test_rejects_package_not_in_whitelist(self, db, monkeypatch):
        def _fail_if_called(*args, **kwargs):
            raise AssertionError("非白名单包不应触发 npm install")

        monkeypatch.setattr(installer_mod, "_run_npm_install", _fail_if_called)
        result = install_sandbox(
            db, "evil-server", "npm:evil-package", "1.0.0", "stdio",
            ["hack"], {"name": "evil-server"},
        )
        assert result["ok"] is False
        assert result["installed"] is False
        assert "whitelist" in result["error"]

    def test_rejects_non_npm_ref(self, db):
        result = install_sandbox(
            db, "x", "pip:something", "1.0.0", "stdio", [], {"name": "x"},
        )
        assert result["ok"] is False

    def test_rejects_non_stdio_transport(self, db):
        result = install_sandbox(
            db, "x", "npm:@modelcontextprotocol/server-memory", "1.0.0", "sse",
            [], {"name": "x"},
        )
        assert result["ok"] is False

    def test_success_records_launch_config(self, db, monkeypatch, tmp_path):
        entry = tmp_path / "index.js"
        entry.write_text("// fake entry", encoding="utf-8")

        def _fake_npm(package_name, version, sandbox_dir):
            return {"ok": True, "entry_js": str(entry), "error": None,
                    "sandbox_path": str(sandbox_dir)}

        monkeypatch.setattr(installer_mod, "_run_npm_install", _fake_npm)
        result = install_sandbox(
            db,
            "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory",
            "0.1.0", "stdio",
            ["create_entities", "read_graph"],
            {"name": "@modelcontextprotocol/server-memory"},
            launch_args=["--flag"],
            env_keys=["SOME_KEY"],
        )
        assert result["ok"] is True and result["installed"] is True
        assert result["entry_js"] == str(entry)

        from aiive.db.models import Capability, MCPInstallRecord
        cap = db.query(Capability).filter(
            Capability.capability_id == "mcp:@modelcontextprotocol/server-memory"
        ).one()
        assert cap.state == "sandbox"
        launch = cap.definition["launch"]
        assert launch["runner"] == "node"
        assert launch["entry_js"] == str(entry)
        assert launch["args"] == ["--flag"]
        assert launch["env_keys"] == ["SOME_KEY"]
        install = db.query(MCPInstallRecord).filter(
            MCPInstallRecord.capability_id == cap.id
        ).one()
        assert "/tmp/" not in (install.sandbox_path or "")

    def test_npm_failure_returns_honest_error(self, db, monkeypatch):
        def _fake_npm(package_name, version, sandbox_dir):
            return {"ok": False, "entry_js": None,
                    "error": "npm install exit=1: boom",
                    "sandbox_path": str(sandbox_dir)}

        monkeypatch.setattr(installer_mod, "_run_npm_install", _fake_npm)
        result = install_sandbox(
            db, "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory", "0.1.0", "stdio",
            [], {"name": "x"},
        )
        assert result["ok"] is False
        assert "npm install exit=1" in result["error"]

    def test_reinstall_requires_a_fresh_smoke(self, db, monkeypatch, tmp_path):
        entry = tmp_path / "index.js"
        entry.write_text("// fake entry", encoding="utf-8")
        monkeypatch.setattr(
            installer_mod,
            "_run_npm_install",
            lambda *_args: {
                "ok": True,
                "entry_js": str(entry),
                "error": None,
                "sandbox_path": str(tmp_path),
            },
        )
        args = (
            db,
            "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory",
            "1.0.0",
            "stdio",
            ["read_graph"],
            {"name": "memory"},
        )
        first = install_sandbox(*args)
        run_smoke(db, first["capability_id"], {"ok": True, "real_tools": ["read_graph"]})
        second = install_sandbox(*args)
        assert second["state"] == "needs_review"


class TestRunSmoke:
    def _install(self, db, monkeypatch, tmp_path):
        entry = tmp_path / "index.js"
        entry.write_text("//", encoding="utf-8")
        monkeypatch.setattr(
            installer_mod, "_run_npm_install",
            lambda p, v, d: {"ok": True, "entry_js": str(entry), "error": None,
                             "sandbox_path": str(d)},
        )
        install_sandbox(
            db, "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory", "0.1.0", "stdio",
            ["read_graph"], {"name": "x"},
        )

    def test_pass_promotes_to_active(self, db, monkeypatch, tmp_path):
        self._install(db, monkeypatch, tmp_path)
        result = run_smoke(
            db, "mcp:@modelcontextprotocol/server-memory",
            {"ok": True, "real_tools": ["read_graph"]},
        )
        assert result["smoke_passed"] is True
        assert result["state"] == "active"

    def test_fail_then_retry_from_needs_review(self, db, monkeypatch, tmp_path):
        self._install(db, monkeypatch, tmp_path)
        cap_id = "mcp:@modelcontextprotocol/server-memory"
        first = run_smoke(db, cap_id, {"ok": False, "error": "spawn failed"})
        assert first["state"] == "needs_review"
        # 修复单向死路：needs_review 状态允许再次冒烟并成功转 active
        second = run_smoke(db, cap_id, {"ok": True, "real_tools": ["read_graph"]})
        assert second["state"] == "active"

    def test_active_state_rejects_smoke(self, db, monkeypatch, tmp_path):
        self._install(db, monkeypatch, tmp_path)
        cap_id = "mcp:@modelcontextprotocol/server-memory"
        run_smoke(db, cap_id, {"ok": True, "real_tools": ["read_graph"]})
        third = run_smoke(db, cap_id, {"ok": True})
        assert third["ok"] is False


# ---------------------------------------------------------------------------
# runtime_client（真实协议，Python 假 server 夹具）
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_client():
    from aiive.mcp.runtime_client import MCPLaunchSpec, MCPRuntimeClient

    client = MCPRuntimeClient(idle_timeout_seconds=30.0)
    spec = MCPLaunchSpec(
        command=sys.executable,
        args=(str(_FIXTURE_SERVER),),
    )
    client.configure("mcp:test-fake", spec)
    yield client
    client.close()


class TestRuntimeClient:
    def test_list_tools_real_protocol(self, mcp_client):
        tools = mcp_client.list_tools("mcp:test-fake", timeout=30, startup_timeout=60)
        names = {t["name"] for t in tools}
        assert names == {"echo", "boom"}
        echo_tool = next(t for t in tools if t["name"] == "echo")
        assert "text" in echo_tool["input_schema"].get("properties", {})

    def test_call_tool_success_and_untrusted_flag(self, mcp_client):
        result = mcp_client.call_tool(
            "mcp:test-fake", "echo", {"text": "hi"}, timeout=30,
        )
        assert result.ok is True
        assert result.untrusted is True
        assert any("echo:hi" in part for part in result.result["content"])

    def test_call_tool_error_propagates_is_error(self, mcp_client):
        result = mcp_client.call_tool("mcp:test-fake", "boom", {}, timeout=30)
        assert result.ok is False
        assert result.untrusted is True
        assert result.error

    def test_unconfigured_capability_raises(self, mcp_client):
        with pytest.raises(RuntimeError, match="not configured"):
            mcp_client.list_tools("mcp:nowhere", timeout=5, startup_timeout=5)

    def test_tool_result_is_untrusted_contract(self, mcp_client):
        from aiive.mcp.runtime_client import tool_result_is_untrusted

        assert mcp_client.tool_result_is_untrusted() is True
        assert tool_result_is_untrusted() is True


class TestBuildLaunchSpec:
    def test_rejects_missing_entry(self):
        from aiive.mcp.runtime_client import build_launch_spec

        with pytest.raises(ValueError):
            build_launch_spec({"runner": "node", "entry_js": "Z:/no/such/file.js"})

    def test_rejects_arbitrary_runner(self):
        from aiive.mcp.runtime_client import build_launch_spec

        with pytest.raises(ValueError, match="unsupported MCP runner"):
            build_launch_spec({"runner": "cmd.exe", "entry_js": str(_FIXTURE_SERVER)})


# ---------------------------------------------------------------------------
# bootstrap → ToolRegistry 注册
# ---------------------------------------------------------------------------

class _StubRuntime:
    """替代真实 MCP 运行时的 stub，记录调用。"""

    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_tool(self, capability_id, tool_name, arguments, timeout=60.0):
        from aiive.mcp.runtime_client import MCPToolResult

        self.calls.append((capability_id, tool_name, dict(arguments)))
        return MCPToolResult(
            ok=True, result={"content": [f"ran {tool_name}"]}, untrusted=True,
        )


class TestBootstrapRegistration:
    def test_register_capability_tools_into_registry(self, monkeypatch):
        from aiive.mcp import bootstrap
        from aiive.tools.registry import ToolRegistry

        stub = _StubRuntime()
        monkeypatch.setattr(bootstrap, "get_runtime_client", lambda: stub)

        registry = ToolRegistry()
        tools = [
            {
                "name": "read_graph",
                "description": "Read the knowledge graph",
                "input_schema": {
                    "type": "object",
                    "properties": {"depth": {"type": "integer", "description": "深度"}},
                },
            },
            {
                "name": "delete_entities",
                "description": "Delete entities",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        registered = bootstrap.register_capability_tools(
            registry,
            capability_name="@modelcontextprotocol/server-memory",
            db_capability_id="mcp:@modelcontextprotocol/server-memory",
            tools=tools,
            base_risk_level="low",
            trust_level="untrusted",
        )
        assert registered == [
            "mcp_server_memory_read_graph",
            "mcp_server_memory_delete_entities",
        ]

        read_reg = registry.get("mcp_server_memory_read_graph")
        assert read_reg is not None
        assert read_reg.safety.definition_source == "remote_mcp"
        assert read_reg.safety.definition_trust_level == "untrusted"
        assert read_reg.safety.writes_external_world is False
        assert read_reg.parameters["depth"]["type"] == "int"

        del_reg = registry.get("mcp_server_memory_delete_entities")
        assert del_reg.safety.can_delete is True
        assert del_reg.safety.writes_external_world is True
        assert del_reg.safety.risk_level == "high"
        assert del_reg.safety.requires_confirmation is True
        assert del_reg.safety.effect_mode == "non_repeatable_external"

    def test_registered_handler_wraps_untrusted_result(self, monkeypatch):
        from aiive.mcp import bootstrap
        from aiive.tools.registry import ToolRegistry

        stub = _StubRuntime()
        monkeypatch.setattr(bootstrap, "get_runtime_client", lambda: stub)

        registry = ToolRegistry()
        bootstrap.register_capability_tools(
            registry,
            capability_name="@modelcontextprotocol/server-memory",
            db_capability_id="mcp:@modelcontextprotocol/server-memory",
            tools=[{
                "name": "read_graph", "description": "",
                "input_schema": {"type": "object", "properties": {}},
            }],
            base_risk_level="low",
            trust_level="untrusted",
        )
        # 只读工具走 registry.execute 直接路径
        outcome = registry.execute(
            "mcp_server_memory_read_graph", {}, "trusted_user_command",
        )
        assert outcome["ok"] is True
        inner = outcome["result"]
        assert inner["untrusted"] is True
        assert inner["ok"] is True
        assert stub.calls[0][0] == "mcp:@modelcontextprotocol/server-memory"
        assert stub.calls[0][1] == "read_graph"

    def test_restore_active_capabilities_reads_db(self, db, monkeypatch, tmp_path):
        from aiive.mcp import bootstrap
        from aiive.tools.registry import ToolRegistry

        entry = tmp_path / "index.js"
        entry.write_text("//", encoding="utf-8")
        monkeypatch.setattr(
            installer_mod, "_run_npm_install",
            lambda p, v, d: {"ok": True, "entry_js": str(entry), "error": None,
                             "sandbox_path": str(d)},
        )
        install_sandbox(
            db, "@modelcontextprotocol/server-memory",
            "npm:@modelcontextprotocol/server-memory", "0.1.0", "stdio",
            ["read_graph"], {"name": "x"},
        )
        cap_id = "mcp:@modelcontextprotocol/server-memory"
        # 冒烟通过 + 缓存真实工具定义
        from aiive.db.models import Capability
        cap = db.query(Capability).filter(Capability.capability_id == cap_id).one()
        definition = dict(cap.definition)
        definition["tools"] = [{
            "name": "read_graph", "description": "",
            "input_schema": {"type": "object", "properties": {}},
        }]
        definition["risk_verdict"] = "low"
        definition["trust_level"] = "untrusted"
        cap.definition = definition
        run_smoke(db, cap_id, {"ok": True, "real_tools": ["read_graph"]})
        db.commit()

        stub = _StubRuntime()

        class _StubConfigurable(_StubRuntime):
            def configure(self, capability_id, spec):
                pass

        stub = _StubConfigurable()
        monkeypatch.setattr(bootstrap, "get_runtime_client", lambda: stub)
        # node 可执行不一定存在于 CI：build_launch_spec 也 stub 掉
        monkeypatch.setattr(bootstrap, "build_launch_spec", lambda launch: object())

        registry = ToolRegistry()
        summary = bootstrap.restore_active_capabilities(registry)
        assert summary["restored"] == 1
        assert summary["failures"] == []
        assert registry.get("mcp_server_memory_read_graph") is not None
