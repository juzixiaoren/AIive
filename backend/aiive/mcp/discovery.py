"""
MCP 服务发现模块：管理和搜索 MCP（Model Context Protocol）服务器候选列表。
提供内置的 MCP 服务目录，支持根据用户目标关键词进行匹配和搜索。
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPServerCandidate:
    """MCP 服务器候选条目。

    属性:
        name: 服务器名称（如 @modelcontextprotocol/server-filesystem）
        source: 来源（official_reference | official_registry | community | user_config）
        version: 版本号
        description: 功能描述
        transport: 通信传输方式（stdio | sse | streamable_http）
        package_ref: 包引用（npm:xxx 或 pip:xxx）
        declared_tools: 声明的工具列表
        risk_notes: 风险提示
        definition_trust_level: 信任等级（semi_trusted | untrusted）
        descriptor_hash: 描述符哈希
        required_env: 该 server 运行所需的环境变量名列表（如 API key）
        match_score: 与搜索目标的关键词匹配得分（搜索时填充）
    """
    name: str
    source: str  # 来源：official_reference | official_registry | community | user_config
    version: str
    description: str
    transport: str  # 传输方式：stdio | sse | streamable_http
    package_ref: str  # 包引用：npm package 或 pip package
    declared_tools: list[str] = field(default_factory=list)
    risk_notes: str = ""
    definition_trust_level: str = "untrusted"
    descriptor_hash: str = ""
    required_env: list[str] = field(default_factory=list)
    homepage: str = ""
    installable: bool = True
    match_score: int = 0


def _compute_hash(candidate: dict[str, Any]) -> str:
    """计算候选条目的 SHA256 哈希（取前 16 位）。

    参数:
        candidate: 候选条目字典

    返回:
        16 位十六进制哈希字符串
    """
    raw = json.dumps(candidate, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# 内置 MCP 预设只保留上游仍维护的 reference servers。更广泛的第三方能力应
# 经官方 Registry discovery + 人工审查进入，而不是继续推荐已归档的软件包。
_BUILTIN_CATALOG: list[dict[str, Any]] = [
    {
        "name": "@modelcontextprotocol/server-filesystem",
        "source": "official_reference",
        "version": "2026.7.10",
        "description": "Secure file operations with explicit allowed roots / 安全文件读写与目录访问",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-filesystem",
        "declared_tools": ["read_file", "write_file", "create_directory", "list_directory", "move_file", "search_files", "get_file_info"],
        "risk_notes": "Filesystem access becomes high risk without explicit launch roots.",
        "aliases": ["file", "files", "document", "文件", "文档", "目录"],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    },
    {
        "name": "@modelcontextprotocol/server-memory",
        "source": "official_reference",
        "version": "2026.7.4",
        "description": "Knowledge graph memory reference server / 知识图谱记忆服务",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-memory",
        "declared_tools": ["create_entities", "create_relations", "add_observations", "delete_entities", "read_graph", "search_nodes"],
        "risk_notes": "Can mutate and delete its own graph; keep separate from AIive authoritative memory.",
        "aliases": ["memory", "knowledge graph", "记忆", "知识图谱"],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    },
    {
        "name": "@modelcontextprotocol/server-sequential-thinking",
        "source": "official_reference",
        "version": "2026.7.4",
        "description": "Reference server for structured sequential thinking / 结构化分步思考",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-sequential-thinking",
        "declared_tools": ["sequentialthinking"],
        "risk_notes": "No external write is expected; output remains untrusted MCP content.",
        "aliases": ["thinking", "reasoning", "思考", "推理"],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
    },
    {
        "name": "@modelcontextprotocol/server-everything",
        "source": "official_reference",
        "version": "2026.7.4",
        "description": "MCP protocol reference/test server for diagnostics / MCP 协议诊断服务",
        "transport": "stdio",
        "package_ref": "npm:@modelcontextprotocol/server-everything",
        "declared_tools": ["echo", "add", "longRunningOperation", "sampleLLM"],
        "risk_notes": "Diagnostic reference server; do not expose sampling or arbitrary test features in production.",
        "aliases": ["test", "diagnostic", "protocol", "测试", "诊断"],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
    },
]


def search_mcp_candidates(goal: str) -> list[MCPServerCandidate]:
    """根据用户目标搜索匹配的 MCP 候选服务器。

    使用简单的关键词匹配评分：
    - 命中名称：+3 分
    - 命中描述：+2 分
    - 命中工具名：+1 分

    结果按匹配得分降序排列（历史缺陷：曾计算 score 却未排序，
    调用方盲取第一个会拿到目录顺序而非最佳匹配）。

    参数:
        goal: 用户目标描述文本

    返回:
        按 match_score 降序排列的 MCPServerCandidate 列表
    """
    goal_lower = goal.lower()
    results: list[MCPServerCandidate] = []

    for entry in _BUILTIN_CATALOG:
        name_lower = entry["name"].lower()
        desc_lower = entry["description"].lower()
        tools_text = " ".join(entry["declared_tools"]).lower()
        aliases_text = " ".join(entry.get("aliases", [])).lower()

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
            if kw in aliases_text:
                score += 2

        # 任一关键词匹配即包含
        if score > 0 or not keywords:
            candidate_data = dict(entry)
            # 根据来源确定信任等级
            candidate_data["definition_trust_level"] = (
                "semi_trusted"
                if entry["source"] in {"official_reference", "official_registry"}
                else "untrusted"
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
                required_env=list(entry.get("required_env", [])),
                homepage=str(entry.get("homepage", "")),
                installable=bool(entry.get("installable", True)),
                match_score=score,
            ))

    # 按得分降序，得分相同保持目录顺序（sorted 稳定）
    results.sort(key=lambda c: c.match_score, reverse=True)
    return results


def get_candidate_by_name(name: str) -> MCPServerCandidate | None:
    """按精确名称从目录中取出候选（不存在返回 None）。"""
    for candidate in search_mcp_candidates(""):
        if candidate.name == name:
            return candidate
    return None


def allowed_npm_packages() -> set[str]:
    """返回允许通过 npm 安装的包名白名单（来自内置目录）。

    installer 只接受该白名单内的包名，防止任意包安装。
    """
    packages: set[str] = set()
    for entry in _BUILTIN_CATALOG:
        ref = str(entry.get("package_ref", ""))
        if ref.startswith("npm:"):
            packages.add(ref[len("npm:"):])
    return packages
