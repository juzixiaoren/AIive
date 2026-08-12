"""测试 MCP 服务发现——搜索候选服务器及其元数据。"""
from aiive.mcp.discovery import MCPServerCandidate, search_mcp_candidates


class TestMCPServerCandidate:
    """测试 MCPServerCandidate 数据模型的所有字段。"""

    def test_all_fields(self):
        """验证 MCPServerCandidate 各字段赋值和读取正确。"""
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
    """测试 MCP 候选服务器的搜索和结果质量。"""

    def test_search_returns_results_for_goal(self):
        """验证按目标搜索能返回相关结果。"""
        results = search_mcp_candidates("sequential thinking")
        assert len(results) > 0
        names = [r.name for r in results]
        assert any("sequential-thinking" in n.lower() for n in names)

    def test_search_returns_results_for_file(self):
        """验证按文件类型搜索能返回结果。"""
        results = search_mcp_candidates("read files")
        assert len(results) > 0

    def test_search_returns_results_for_database(self):
        """验证按数据库类型搜索能返回结果。"""
        results = search_mcp_candidates("reasoning")
        assert len(results) > 0

    def test_results_have_descriptor_hash(self):
        """验证搜索结果都包含描述符哈希。"""
        results = search_mcp_candidates("search")
        for r in results:
            assert r.descriptor_hash
            assert len(r.descriptor_hash) == 16

    def test_results_have_trust_level(self):
        """验证搜索结果都包含可信等级。"""
        results = search_mcp_candidates("filesystem")
        for r in results:
            assert r.definition_trust_level in ("semi_trusted", "untrusted")

    def test_results_have_risk_notes(self):
        """验证搜索结果都包含风险说明。"""
        results = search_mcp_candidates("memory")
        for r in results:
            assert r.risk_notes

    def test_official_reference_catalog_is_semi_trusted(self):
        """验证官方 reference catalog 的结果可信等级为 semi_trusted。"""
        results = search_mcp_candidates("filesystem")
        official = [r for r in results if r.source == "official_reference"]
        for r in official:
            assert r.definition_trust_level == "semi_trusted"

    def test_catalog_only_recommends_maintained_installable_presets(self):
        """内置预设不再推荐已经归档的 Brave/GitHub/Postgres/Puppeteer 包。"""
        results = search_mcp_candidates("")
        assert len(results) == 4
        assert all(r.source == "official_reference" and r.installable for r in results)
        names = {r.name for r in results}
        assert not any(token in " ".join(names) for token in ("github", "brave", "postgres", "puppeteer"))

    def test_results_are_mcpservercandidate_instances(self):
        """验证搜索结果均为 MCPServerCandidate 实例。"""
        results = search_mcp_candidates("filesystem")
        for r in results:
            assert isinstance(r, MCPServerCandidate)
