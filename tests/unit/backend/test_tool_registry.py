from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    compute_descriptor_hash,
    get_tool_registry,
)


class TestCapabilitySafetySchema:
    def test_all_fields(self):
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
        schema = CapabilitySafetySchema(
            capability_id="x",
            definition_source="remote_mcp",
            definition_trust_level="untrusted",
            risk_level="high",
        )
        assert schema.requires_confirmation is False  # not auto-enforced
        assert schema.allowed_instruction_sources == ["trusted_user_command"]


class TestToolRegistry:
    def test_register_and_get(self):
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

    def test_execute_unknown_tool(self):
        registry = ToolRegistry()
        result = registry.execute("nonexistent", {}, "trusted_user_command")
        assert result["ok"] is False

    def test_execute_untrusted_source_blocked(self):
        registry = ToolRegistry()

        def handler(**kw):
            return "executed"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("t1", "local_builtin", "trusted", "low"),
            handler=handler,
        ))
        result = registry.execute("t1", {}, "untrusted_external_content")
        assert result["ok"] is False
        assert "not authorized" in result["error"].lower()

    def test_execute_requires_confirmation(self):
        registry = ToolRegistry()

        def handler(**kw):
            return "should not run"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema(
                "risky", "local_builtin", "trusted", "medium",
                requires_confirmation=True,
            ),
            handler=handler,
        ))
        result = registry.execute("risky", {}, "trusted_user_command")
        assert result["ok"] is False
        assert result["approval_required"] is True

    def test_execute_succeeds_for_safe_tool(self):
        registry = ToolRegistry()

        def handler(message: str = "") -> str:
            return f"echo: {message}"

        registry.register(ToolRegistration(
            safety=CapabilitySafetySchema("echo", "local_builtin", "trusted", "low"),
            handler=handler,
        ))
        result = registry.execute("echo", {"message": "hello"}, "trusted_user_command")
        assert result["ok"] is True
        assert result["result"] == "echo: hello"


class TestBuiltinTools:
    def test_registry_has_echo_and_read_file(self):
        registry = get_tool_registry()
        tools = registry.list_all()
        ids = {t["capability_id"] for t in tools}
        assert "echo" in ids
        assert "read_text_file_limited" in ids

    def test_echo_executes(self):
        registry = get_tool_registry()
        result = registry.execute("echo", {"message": "hello world"}, "trusted_user_command")
        assert result["ok"] is True
        assert result["result"] == "hello world"

    def test_read_file_requires_confirmation(self):
        registry = get_tool_registry()
        tool = [t for t in registry.list_all() if t["capability_id"] == "read_text_file_limited"][0]
        assert tool["requires_confirmation"] is True
        assert tool["risk_level"] == "medium"


class TestDescriptorHash:
    def test_hash_stable_for_same_input(self):
        schema1 = {"capability_id": "x", "risk_level": "low"}
        schema2 = {"capability_id": "x", "risk_level": "low"}
        assert compute_descriptor_hash(schema1) == compute_descriptor_hash(schema2)

    def test_hash_differs_for_different_input(self):
        assert compute_descriptor_hash({"a": 1}) != compute_descriptor_hash({"a": 2})
