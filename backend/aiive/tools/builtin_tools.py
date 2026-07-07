import os

from aiive.tools.registry import (
    CapabilitySafetySchema,
    ToolRegistration,
    ToolRegistry,
    compute_descriptor_hash,
)


def _build_safety(capability_id: str, **overrides) -> CapabilitySafetySchema:
    base = {
        "capability_id": capability_id,
        "definition_source": "local_builtin",
        "definition_trust_level": "trusted",
        "risk_level": "low",
        "requires_confirmation": False,
        "writes_external_world": False,
        "can_access_secret": False,
        "can_delete": False,
    }
    base.update(overrides)
    base["descriptor_hash"] = compute_descriptor_hash(base)
    return CapabilitySafetySchema(**{k: v for k, v in base.items() if k in CapabilitySafetySchema.__dataclass_fields__})


def _handle_echo(message: str) -> str:
    return message


def _handle_read_text_file_limited(path: str, max_lines: int = 50) -> dict:
    allowed_dir = os.path.expanduser("~/Documents")
    real_path = os.path.realpath(path)

    if not real_path.startswith(allowed_dir):
        return {"error": "Access denied: path outside ~/Documents", "path": path}

    if not os.path.isfile(real_path):
        return {"error": "File not found", "path": path}

    try:
        with open(real_path, "r") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    lines.append(f"... (truncated at {max_lines} lines)")
                    break
                lines.append(line.rstrip("\n"))
            return {"lines": lines, "total_read": len([l for l in lines if not l.startswith("...")])}
    except Exception as e:
        return {"error": str(e), "path": path}


def register_builtin_tools(registry: ToolRegistry) -> None:
    registry.register(ToolRegistration(
        safety=_build_safety("echo"),
        handler=_handle_echo,
        description="Echo back the input message",
        parameters={"message": "string"},
    ))

    registry.register(ToolRegistration(
        safety=_build_safety(
            "read_text_file_limited",
            risk_level="medium",
            requires_confirmation=True,
        ),
        handler=_handle_read_text_file_limited,
        description="Read a text file within ~/Documents (max 50 lines, requires confirmation)",
        parameters={"path": "string", "max_lines": "int"},
    ))
