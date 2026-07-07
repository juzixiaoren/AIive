from fastapi import APIRouter
from pydantic import BaseModel, Field

from aiive.mcp.discovery import search_mcp_candidates

router = APIRouter(prefix="/api")


class MCPSearchRequest(BaseModel):
    goal: str = Field(..., min_length=1)


@router.post("/mcp/search")
def search_mcp(request: MCPSearchRequest):
    candidates = search_mcp_candidates(request.goal)
    return [
        {
            "name": c.name,
            "source": c.source,
            "version": c.version,
            "description": c.description,
            "transport": c.transport,
            "package_ref": c.package_ref,
            "declared_tools": c.declared_tools,
            "risk_notes": c.risk_notes,
            "definition_trust_level": c.definition_trust_level,
            "descriptor_hash": c.descriptor_hash,
        }
        for c in candidates
    ]


@router.get("/capabilities")
def list_capabilities(state: str = "candidate"):
    # Placeholder: return MCP candidates as capabilities
    all_candidates = search_mcp_candidates("")
    return [
        {
            "capability_id": f"mcp:{c.name}",
            "name": c.name,
            "state": "candidate",
            "definition_trust_level": c.definition_trust_level,
            "description": c.description,
            "declared_tools": c.declared_tools,
            "descriptor_hash": c.descriptor_hash,
        }
        for c in all_candidates
    ]
