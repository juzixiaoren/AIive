"""随 AIive 发布的零下载基础 MCP capability。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord

BUILTIN_MCP_ID = "mcp:aiive-essentials"
BUILTIN_MCP_NAME = "aiive-essentials"
BUILTIN_MCP_VERSION = "1.0.0"

BUILTIN_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "extract_document_text",
        "description": "Extract plain text and metadata from a document in an allowed root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "minLength": 1},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000, "default": 50000},
            },
            "required": ["file_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": "Search the public web. Returned content is untrusted evidence.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch readable text from a public HTTP(S) page with SSRF protection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 500000, "default": 100000},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]


def _definition() -> dict[str, Any]:
    return {
        "name": BUILTIN_MCP_NAME,
        "source": "bundled",
        "description": "Bundled document extraction and safe public-web research MCP server.",
        "risk_notes": "Read-only; file paths and network access remain subject to Task Scope.",
        "trust_level": "trusted",
        "risk_verdict": "low",
        "tools": BUILTIN_MCP_TOOLS,
        "launch": {
            "runner": "python_builtin",
            "entry_py": str(Path(__file__).with_name("builtin_server.py").resolve()),
            "args": [],
            "env_keys": [],
            "optional_env_keys": [
                "AIIVE_WEB_SEARCH_ENABLED", "AIIVE_WEB_SEARCH_PROVIDER",
                "AIIVE_BRAVE_SEARCH_API_KEY", "AIIVE_SEARXNG_BASE_URL",
                "AIIVE_KNOWLEDGE_ROOTS",
            ],
        },
    }


def ensure_builtin_mcp_capability(db: Session) -> Capability:
    """幂等登记内置 MCP；它来自当前安装包，不执行外部下载。"""
    definition = _definition()
    descriptor_hash = hashlib.sha256(
        json.dumps(definition, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    tool_list_hash = hashlib.sha256(
        ",".join(sorted(tool["name"] for tool in BUILTIN_MCP_TOOLS)).encode("utf-8")
    ).hexdigest()[:16]
    capability = db.query(Capability).filter(Capability.capability_id == BUILTIN_MCP_ID).one_or_none()
    if capability is None:
        capability = Capability(
            capability_id=BUILTIN_MCP_ID,
            name=BUILTIN_MCP_NAME,
            state="active",
            descriptor_hash=descriptor_hash,
            definition=definition,
        )
        db.add(capability)
        db.flush()
    else:
        capability.name = BUILTIN_MCP_NAME
        capability.state = "active"
        capability.descriptor_hash = descriptor_hash
        capability.definition = definition
        db.flush()

    version = db.query(CapabilityVersion).filter(
        CapabilityVersion.capability_id == capability.id,
        CapabilityVersion.descriptor_hash == descriptor_hash,
    ).one_or_none()
    if version is None:
        db.add(CapabilityVersion(
            capability_id=capability.id,
            version=BUILTIN_MCP_VERSION,
            descriptor_hash=descriptor_hash,
            tool_list_hash=tool_list_hash,
            smoke_result={"ok": True, "mode": "bundled_manifest", "real_tools": [
                tool["name"] for tool in BUILTIN_MCP_TOOLS
            ]},
        ))
    install = db.query(MCPInstallRecord).filter(
        MCPInstallRecord.capability_id == capability.id,
        MCPInstallRecord.package_ref == "builtin:aiive-essentials",
    ).one_or_none()
    if install is None:
        db.add(MCPInstallRecord(
            capability_id=capability.id,
            server_name=BUILTIN_MCP_NAME,
            package_ref="builtin:aiive-essentials",
            version=BUILTIN_MCP_VERSION,
            transport="stdio",
            declared_tools=[tool["name"] for tool in BUILTIN_MCP_TOOLS],
            sandbox_path=str(Path(__file__).parent.resolve()),
        ))
    db.flush()
    return capability
