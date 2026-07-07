import hashlib
import json
from typing import Optional

from sqlalchemy.orm import Session

from aiive.db.models import Capability, CapabilityVersion, MCPInstallRecord


def _hash(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def install_sandbox(
    db: Session,
    candidate_name: str,
    package_ref: str,
    version: str,
    transport: str,
    declared_tools: list[str],
    definition: dict,
) -> dict:
    capability_id = f"mcp:{candidate_name}"
    descriptor_hash = _hash(json.dumps(definition, sort_keys=True))
    tool_list_hash = _hash(",".join(sorted(declared_tools)))

    # Check existing capability
    existing = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )

    if existing:
        if existing.descriptor_hash and existing.descriptor_hash != descriptor_hash:
            existing.state = "needs_review"
            db.flush()
        cap = existing
    else:
        cap = Capability(
            capability_id=capability_id,
            name=candidate_name,
            state="sandbox",
            descriptor_hash=descriptor_hash,
            definition=definition,
        )
        db.add(cap)
        db.flush()

    # Record install
    install = MCPInstallRecord(
        capability_id=cap.id,
        server_name=candidate_name,
        package_ref=package_ref,
        version=version,
        transport=transport,
        declared_tools=list(declared_tools),
        sandbox_path=f"/tmp/aiive_sandbox/{candidate_name}",
    )
    db.add(install)

    # Record version
    cv = CapabilityVersion(
        capability_id=cap.id,
        version=version,
        descriptor_hash=descriptor_hash,
        tool_list_hash=tool_list_hash,
    )
    db.add(cv)
    db.flush()

    return {
        "capability_id": cap.capability_id,
        "state": cap.state,
        "descriptor_hash": descriptor_hash,
        "tool_list_hash": tool_list_hash,
        "installed": True,
    }


def run_smoke(
    db: Session,
    capability_id: str,
    smoke_result: dict,
) -> dict:
    cap = (
        db.query(Capability)
        .filter(Capability.capability_id == capability_id)
        .first()
    )
    if not cap:
        return {"ok": False, "error": f"Capability not found: {capability_id}"}

    if cap.state != "sandbox":
        return {"ok": False, "error": f"Capability must be in sandbox, current: {cap.state}"}

    # Update latest version with smoke result
    version = (
        db.query(CapabilityVersion)
        .filter(CapabilityVersion.capability_id == cap.id)
        .order_by(CapabilityVersion.created_at.desc())
        .first()
    )
    if version:
        version.smoke_result = smoke_result

    if smoke_result.get("ok") is True:
        cap.state = "active"
    else:
        cap.state = "needs_review"

    db.flush()
    return {
        "ok": True,
        "capability_id": cap.capability_id,
        "state": cap.state,
        "smoke_passed": smoke_result.get("ok") is True,
    }
