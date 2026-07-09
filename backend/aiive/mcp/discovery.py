"""
MCP 服务发现模块：管理和搜索 MCP（Model Context Protocol）服务器候选列表。
提供内置的 MCP 服务目录，支持根据用户目标关键词进行匹配和搜索。
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCPServerCandidate:
    """MCP 服务器候选条目。

    属性:
        name: 服务器名称（如 @modelcontextprotocol/server-filesystem）
        source: 来源（official_registry | community | user_config）
        version: 版本号
        description: 功能描述
        transport: 通信传输方式（stdio | sse | streamable_http）
        package_ref: 包引用（npm:xxx 或 pip:xxx）
        declared_tools: 声明的工具列表
        risk_notes: 风险提示
        definition_trust_level: 信任等级（semi_trusted | untrusted）
        descriptor_hash: 描述符哈希
    """
    name: str
    source: str  # 来源：official_registry | community | user_config
    version: str
    description: str
    transport: str  # 传输方式：stdio | sse | streamable_http
    package_ref: str  # 包引用：npm package 或 pip package
    declared_tools: list[str] = field(default_factory=list)
    risk_notes: str = ""
    definition_trust_level: str = "untrusted"
    descriptor_hash: str = ""


def _compute_hash(candidate: dict) -> str:
    """计算候选条目的 SHA256 哈希（取前 16 位）。

    参数:
        candidate: 候选条目字典

    返回:
        16 位十六进制哈希字符串
    """
    raw = json.dumps(candidate, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# 内置 MCP 目录（模拟注册表）
_BUILTIN_CATALOG: list[dict] = [
    {
        "name": "@modelcontextprotocol/server-filesystem",
        "source": "official_registry",
        "version": "0.6.0",
        "description": "Secure file system operations with configurable access controls",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-filesystem",
        "declared_tools": ["read_file", "write_file", "create_directory", "list_directory", "move_file", "search_files", "get_file_info"],
        "risk_notes": "Filesystem access: risk=high if scope unrestricted. Use scope registry.",
    },
    {
        "name": "@modelcontextprotocol/server-github",
        "source": "official_registry",
        "version": "0.5.0",
        "description": "GitHub API integration for repository management, issues, and PRs",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-github",
        "declared_tools": ["create_or_update_file", "search_repositories", "create_repository", "get_file_contents", "create_issue", "create_pull_request", "list_commits"],
        "risk_notes": "GitHub API access: can read/write repositories. Requires token.",
    },
    {
        "name": "@modelcontextprotocol/server-postgres",
        "source": "official_registry",
        "version": "0.1.0",
        "description": "PostgreSQL database access with read-only query capabilities",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-postgres",
        "declared_tools": ["query"],
        "risk_notes": "Database access: can execute arbitrary SQL. Use read-only user if possible.",
    },
    {
        "name": "@modelcontextprotocol/server-brave-search",
        "source": "official_registry",
        "version": "0.1.0",
        "description": "Web search using Brave Search API",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-brave-search",
        "declared_tools": ["brave_web_search", "brave_local_search"],
        "risk_notes": "External web search: sends queries to Brave API. Requires API key. Low write risk.",
    },
    {
        "name": "@modelcontextprotocol/server-memory",
        "source": "community",
        "version": "0.1.0",
        "description": "Knowledge graph-based memory system for persistent context",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-memory",
        "declared_tools": ["create_entities", "create_relations", "add_observations", "delete_entities", "read_graph", "search_nodes"],
        "risk_notes": "Memory management: can delete entities. Use with AIive's own memory system.",
    },
    {
        "name": "@anthropic/mcp-server-puppeteer",
        "source": "community",
        "version": "0.1.0",
        "description": "Browser automation for web scraping and interaction",
        "transport": "stdio",
        "package_ref": "npm:@anthropic/mcp-server-puppeteer",
        "declared_tools": ["puppeteer_navigate", "puppeteer_screenshot", "puppeteer_click", "puppeteer_fill", "puppeteer_select", "puppeteer_hover", "puppeteer_evaluate"],
        "risk_notes": "Browser automation: can interact with any website. High risk for sensitive operations.",
    },
]


def search_mcp_candidates(goal: str) -> list[MCPServerCandidate]:
    """根据用户目标搜索匹配的 MCP 候选服务器。

    使用简单的关键词匹配评分：
    - 命中名称：+3 分
    - 命中描述：+2 分
    - 命中工具名：+1 分

    参数:
        goal: 用户目标描述文本

    返回:
        匹配的 MCPServerCandidate 列表
    """
    goal_lower = goal.lower()
    results: list[MCPServerCandidate] = []

    for entry in _BUILTIN_CATALOG:
        name_lower = entry["name"].lower()
        desc_lower = entry["description"].lower()
        tools_text = " ".join(entry["declared_tools"]).lower()

        score = 0
        # 按关键词匹配评分
        keywords = goal_lower.split()
        for kw in keywords:
            if kw in name_lower:
                score += 3
            if kw in desc_lower:
                score += 2
            if kw in tools_text:
                score += 1

        # 任一关键词匹配即包含
        if score > 0 or not keywords:
            candidate_data = dict(entry)
            # 根据来源确定信任等级
            candidate_data["definition_trust_level"] = (
                "semi_trusted" if entry["source"] == "official_registry" else "untrusted"
            )
            descriptor_hash = _compute_hash(candidate_data)

            results.append(MCPServerCandidate(
                name=entry["name"],
                source=entry["source"],
                version=entry["version"],
                description=entry["description"],
                transport=entry["transport"],
                package_ref=entry["package_ref"],
                declared_tools=list(entry["declared_tools"]),
                risk_notes=entry["risk_notes"],
                definition_trust_level=candidate_data["definition_trust_level"],
                descriptor_hash=descriptor_hash,
            ))

    return results
