from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from aiive.db.base import get_db
from aiive.db.models import Capability
from aiive.mcp.discovery import search_mcp_candidates
from aiive.mcp.installer import install_sandbox, run_smoke

router = APIRouter(prefix="/api/mcp")


class InstallRequest(BaseModel):
    package_ref: str = Field(...)
    version: str = "latest"
    transport: str = "stdio"


class SmokeRequest(BaseModel):
    tool_name: str = Field(...)
    params: dict = {}


@router.post("/{candidate_name}/install-sandbox")
def install_to_sandbox(
    candidate_name: str,
    request: InstallRequest,
    db: Session = Depends(get_db),
):
    candidates = search_mcp_candidates(candidate_name)
    matched = [c for c in candidates if candidate_name.lower() in c.name.lower()]
    if not matched:
        return {"ok": False, "error": f"No MCP candidate found for: {candidate_name}"}

    candidate = matched[0]
    result = install_sandbox(
        db=db,
        candidate_name=candidate.name,
        package_ref=request.package_ref,
        version=request.version,
        transport=request.transport,
        declared_tools=candidate.declared_tools,
        definition={
            "name": candidate.name,
            "source": candidate.source,
            "description": candidate.description,
            "risk_notes": candidate.risk_notes,
        },
    )
    db.commit()
    return result


@router.post("/{capability_id}/smoke")
def smoke_capability(
    capability_id: str,
    request: SmokeRequest,
    db: Session = Depends(get_db),
):
    # Simulate smoke: call tool and check result
    from aiive.mcp.runtime_client import MCPRuntimeClient

    # Build a simple test client with the tool
    client = _build_test_client(request.tool_name)
    tool_result = client.call_tool(request.tool_name, request.params)

    result = run_smoke(
        db=db,
        capability_id=f"mcp:{capability_id}",
        smoke_result={
            "ok": tool_result.ok,
            "tool_name": request.tool_name,
            "result_preview": str(tool_result.result)[:200] if tool_result.ok else None,
            "error": tool_result.error,
        },
    )
    db.commit()
    return result


@router.get("/capabilities")
def list_installed_capabilities(db: Session = Depends(get_db)):
    caps = (
        db.query(Capability)
        .order_by(Capability.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "capability_id": c.capability_id,
            "name": c.name,
            "state": c.state,
            "descriptor_hash": c.descriptor_hash,
            "created_at": c.created_at.isoformat(),
        }
        for c in caps
    ]


def _build_test_client(tool_name: str) -> MCPRuntimeClient:
    from aiive.mcp.runtime_client import MCPRuntimeClient

    client = MCPRuntimeClient()

    # Built-in test tools
    def echo(msg: str = "") -> str:
        return f"echo: {msg}"

    def list_files(path: str = ".") -> list[str]:
        import os
        try:
            return os.listdir(path)[:10]
        except Exception:
            return []

    client.register_tool("echo", echo)
    client.register_tool("list_files", list_files)

    return client
