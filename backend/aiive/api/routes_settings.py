"""Instance-wide tool trust settings and the live capability catalog."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from aiive.control.tool_trust import ToolIdentity, ToolTrustMode, ToolTrustPolicy
from aiive.db.base import get_db
from aiive.db.models import DesktopNode
from aiive.desktop.connection_manager import desktop_connection_manager
from aiive.desktop.registry_overlay import register_desktop_tools
from aiive.runtime.turn_tool_surface import build_turn_tool_registry
from aiive.tools.registry import ToolRegistry, get_tool_registry

router = APIRouter(prefix="/api/settings")
DbSession = Annotated[Session, Depends(get_db)]


class ToolTrustUpdate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    mode: ToolTrustMode
    approval_exempt_capabilities: list[ToolIdentity] | None = None


class ToolTrustCatalogEntry(BaseModel):
    identity: ToolIdentity
    name: str
    description: str
    risk_level: str
    namespace_id: str
    namespace_name: str
    standard_requires_approval: bool
    standard_approval_condition: str | None = None


class ToolTrustResponse(BaseModel):
    mode: ToolTrustMode
    approval_exempt_capabilities: list[ToolIdentity]
    tools: list[ToolTrustCatalogEntry]


def _catalog(db: Session) -> list[ToolTrustCatalogEntry]:
    nodes = db.scalars(select(DesktopNode).where(
        DesktopNode.status == "online", DesktopNode.lease_expires_at > datetime.now(UTC),
    )).all()
    registries = [get_tool_registry().snapshot(), build_turn_tool_registry()]
    for node in nodes:
        if desktop_connection_manager.is_connected(node.id):
            registry = ToolRegistry()
            register_desktop_tools(registry, node)
            registries.append(registry)

    entries: dict[ToolIdentity, ToolTrustCatalogEntry] = {}
    for registry in registries:
        for tool in registry.registrations():
            identity = ToolIdentity.from_tool(tool)
            discovery = tool.definition.discovery
            entries[identity] = ToolTrustCatalogEntry(
                identity=identity,
                name=discovery.raw_name or identity.capability_id,
                description=tool.description,
                risk_level=tool.safety.risk_level,
                namespace_id=discovery.namespace_id,
                namespace_name=discovery.namespace_name,
                standard_requires_approval=ToolTrustPolicy.standard_reason(tool, {}) is not None,
                standard_approval_condition="non_read_only_command" if identity.capability_id == "run_shell" else None,
            )
    return sorted(entries.values(), key=lambda entry: (entry.namespace_name, entry.name, entry.identity.descriptor_hash))


def _response(policy: ToolTrustPolicy, tools: list[ToolTrustCatalogEntry]) -> ToolTrustResponse:
    return ToolTrustResponse(
        mode=policy.mode,
        approval_exempt_capabilities=sorted(policy.approval_exempt_capabilities, key=lambda entry: entry.model_dump_json()),
        tools=tools,
    )


@router.get("/tool-trust")
def get_tool_trust(db: DbSession) -> ToolTrustResponse:
    return _response(ToolTrustPolicy.load(db), _catalog(db))


@router.put("/tool-trust")
def put_tool_trust(request: ToolTrustUpdate, db: DbSession) -> ToolTrustResponse:
    policy = ToolTrustPolicy.load(db)
    tools = _catalog(db)
    exemptions = request.approval_exempt_capabilities
    if exemptions is not None:
        # Existing grants can be retained or removed while their node is offline.
        valid = {tool.identity for tool in tools} | policy.approval_exempt_capabilities
        if set(exemptions) - valid:
            raise HTTPException(status_code=409, detail="tool_trust_catalog_changed")
        exemptions = list(dict.fromkeys(exemptions))
    ToolTrustPolicy.save(db, request.mode, exemptions)
    response = _response(ToolTrustPolicy.load(db), tools)
    db.commit()
    return response
