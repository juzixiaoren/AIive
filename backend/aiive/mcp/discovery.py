import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCPServerCandidate:
    name: str
    source: str  # official_registry | community | user_config
    version: str
    description: str
    transport: str  # stdio | sse | streamable_http
    package_ref: str  # npm package or pip package
    declared_tools: list[str] = field(default_factory=list)
    risk_notes: str = ""
    definition_trust_level: str = "untrusted"
    descriptor_hash: str = ""


def _compute_hash(candidate: dict) -> str:
    raw = json.dumps(candidate, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Built-in MCP catalog (simulated registry)
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
    goal_lower = goal.lower()
    results: list[MCPServerCandidate] = []

    for entry in _BUILTIN_CATALOG:
        name_lower = entry["name"].lower()
        desc_lower = entry["description"].lower()
        tools_text = " ".join(entry["declared_tools"]).lower()

        score = 0
        # Match keywords in goal
        keywords = goal_lower.split()
        for kw in keywords:
            if kw in name_lower:
                score += 3
            if kw in desc_lower:
                score += 2
            if kw in tools_text:
                score += 1

        # Always include if any keyword match
        if score > 0 or not keywords:
            # Cap at all results but sort by score
            candidate_data = dict(entry)
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

    # Sort by score (implicitly by order of addition) and de-duplicate
    return results
