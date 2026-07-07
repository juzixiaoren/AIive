from aiive.mcp.discovery import MCPServerCandidate, search_mcp_candidates


class TestMCPServerCandidate:
    def test_all_fields(self):
        c = MCPServerCandidate(
            name="test-mcp",
            source="official_registry",
            version="1.0.0",
            description="Test MCP server",
            transport="stdio",
            package_ref="npm:test-mcp",
            declared_tools=["tool_a", "tool_b"],
            risk_notes="Low risk",
            definition_trust_level="semi_trusted",
            descriptor_hash="abc123",
        )
        assert c.name == "test-mcp"
        assert c.source == "official_registry"
        assert c.version == "1.0.0"
        assert len(c.declared_tools) == 2
        assert c.definition_trust_level == "semi_trusted"


class TestSearchMCP:
    def test_search_returns_results_for_goal(self):
        results = search_mcp_candidates("github")
        assert len(results) > 0
        names = [r.name for r in results]
        assert any("github" in n.lower() for n in names)

    def test_search_returns_results_for_file(self):
        results = search_mcp_candidates("read files")
        assert len(results) > 0

    def test_search_returns_results_for_database(self):
        results = search_mcp_candidates("database query")
        assert len(results) > 0

    def test_results_have_descriptor_hash(self):
        results = search_mcp_candidates("search")
        for r in results:
            assert r.descriptor_hash
            assert len(r.descriptor_hash) == 16

    def test_results_have_trust_level(self):
        results = search_mcp_candidates("filesystem")
        for r in results:
            assert r.definition_trust_level in ("semi_trusted", "untrusted")

    def test_results_have_risk_notes(self):
        results = search_mcp_candidates("github")
        for r in results:
            assert r.risk_notes

    def test_official_registry_is_semi_trusted(self):
        results = search_mcp_candidates("filesystem")
        official = [r for r in results if r.source == "official_registry"]
        for r in official:
            assert r.definition_trust_level == "semi_trusted"

    def test_community_source_is_untrusted(self):
        results = search_mcp_candidates("memory")
        community = [r for r in results if r.source == "community"]
        for r in community:
            assert r.definition_trust_level == "untrusted"

    def test_results_are_mcpservercandidate_instances(self):
        results = search_mcp_candidates("filesystem")
        for r in results:
            assert isinstance(r, MCPServerCandidate)
