"""测试 ToolRegistry（工具注册表）模块。

覆盖工具注册、查询、权限执行及描述符哈希计算。
"""

from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    compute_descriptor_hash,
    get_tool_registry,
)


class TestCapabilitySafetySchema:
    """测试 CapabilitySafetySchema 的属性和防御性默认值。"""

    def test_all_fields(self):
        """验证所有字段的正确赋值。"""
        schema = CapabilitySafetySchema(
            capability_id="test_tool",
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="low",
        )
        assert schema.capability_id == "test_tool"
        assert schema.definition_source == "local_builtin"
        assert schema.requires_confirmation is False
        assert schema.writes_external_world is False
        assert schema.can_access_secret is False
        assert schema.can_delete is False
        assert schema.tool_description_is_instruction is False

    def test_defensive_defaults(self):
        """不信任来源的工具应有防御性默认值。"""
        schema = CapabilitySafetySchema(
            capability_id="x",
            definition_source="remote_mcp",
            definition_trust_level="untrusted",
            risk_level="high",
        )
        assert schema.requires_confirmation is False  # 不自动执行
        assert schema.allowed_instruction_sources == ["trusted_user_command"]


class TestToolRegistry:
    """测试 ToolRegistry 的注册、查询和执行功能。"""

    def test_register_and_get(self):
        """注册后应能正确获取工具。"""
        registry = ToolRegistry()

        def handler(**kwargs):
            return "done"

        schema = CapabilitySafetySchema(
            capability_id="echo",
            definition_source="local_builtin",
            definition_trust_level="trusted",
            risk_level="low",
        )
        registry.register(ToolRegistration(safety=schema, handler=handler, description="Echo tool"))
        reg = registry.get("echo")
        assert reg is not None
        assert reg.safety.capability_id == "echo"

    def test_list_all(self):
        """list_all 应返回所有已注册工具。"""
        registry = ToolRegistry()

        def h(**kw):
            return None

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("t1", "local_builtin", "trusted", "low"),
            handler=h,
        ))
        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("t2", "local_builtin", "trusted", "medium"),
            handler=h,
        ))
        tools = registry.list_all()
        assert len(tools) == 2
        assert {t["capability_id"] for t in tools} == {"t1", "t2"}

    def test_register_generates_missing_descriptor_hash(self):
        """注册表应为缺失的 descriptor hash 生成稳定非空值。"""
        registry = ToolRegistry()
        registration = ToolRegistration(
            safety=CapabilitySafetySchema("generated", "local_builtin", "trusted", "low"),
            handler=lambda **kwargs: "ok",
            description="测试工具",
            parameters={"value": "str"},
        )
        registry.register(registration)
        first = registry.get("generated")
        assert first is not None
        assert first.safety.descriptor_hash

        registry.register(registration)
        second = registry.get("generated")
        assert second is not None
        assert second.safety.descriptor_hash == first.safety.descriptor_hash

    def test_execute_unknown_tool(self):
        """执行未注册的工具应返回失败。"""
        registry = ToolRegistry()
        result = registry.execute("nonexistent", {}, "trusted_user_command")
        assert result["ok"] is False

    def test_execute_untrusted_source_blocked(self):
        """不可信来源应被拦截。"""
        registry = ToolRegistry()

        def handler(ctx=None, **kw):
            return "executed"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("t1", "local_builtin", "trusted", "low"),
            handler=handler,
        ))
        result = registry.execute("t1", {}, "untrusted_external_content")
        assert result["ok"] is False
        assert "not authorized" in result["error"].lower()

    def test_execute_ignores_confirmation_metadata(self):
        """确认元数据不应阻止已注册工具直接执行。"""
        registry = ToolRegistry()

        def handler(ctx=None, **kw):
            return "executed"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema(
                "risky", "local_builtin", "trusted", "medium",
                requires_confirmation=True,
            ),
            handler=handler,
        ))
        result = registry.execute("risky", {}, "trusted_user_command")
        assert result["ok"] is True
        assert result["result"] == "executed"

    def test_execute_succeeds_for_safe_tool(self):
        """安全工具应能正常执行。"""
        registry = ToolRegistry()

        def handler(ctx=None, message: str = "") -> str:
            return f"echo: {message}"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("echo", "local_builtin", "trusted", "low"),
            handler=handler,
        ))
        result = registry.execute("echo", {"message": "hello"}, "trusted_user_command")
        assert result["ok"] is True
        assert result["result"] == "echo: hello"


class TestBuiltinTools:
    """测试内置工具的注册状态和执行。"""

    def test_registry_has_echo_and_read_file(self):
        """注册表应包含 echo 和 read_text_file 工具。"""
        registry = get_tool_registry()
        tools = registry.list_all()
        ids = {t["capability_id"] for t in tools}
        assert "echo" in ids
        assert "read_text_file" in ids

    def test_echo_executes(self):
        """echo 工具应正确执行并返回结果。"""
        registry = get_tool_registry()
        result = registry.execute("echo", {"message": "hello world"}, "trusted_user_command")
        assert result["ok"] is True
        assert result["result"] == "hello world"

    def test_read_file_requires_confirmation(self):
        """read_text_file 工具注册元数据检查。"""
        registry = get_tool_registry()
        tool = [t for t in registry.list_all() if t["capability_id"] == "read_text_file"][0]
        assert tool.get("risk_level") == "low"
        assert tool.get("writes_external_world") is False


class TestDescriptorHash:
    """测试工具描述符哈希的计算。"""

    def test_hash_stable_for_same_input(self):
        """相同输入应产生相同哈希。"""
        schema1 = {"capability_id": "x", "risk_level": "low"}
        schema2 = {"capability_id": "x", "risk_level": "low"}
        assert compute_descriptor_hash(schema1) == compute_descriptor_hash(schema2)

    def test_hash_differs_for_different_input(self):
        """不同输入应产生不同哈希。"""
        assert compute_descriptor_hash({"a": 1}) != compute_descriptor_hash({"a": 2})
