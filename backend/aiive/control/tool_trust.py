"""Instance-wide human approval policy shared by both agents."""
from __future__ import annotations

from typing import Any, ClassVar, Literal, override

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aiive.db.models import ToolTrustSetting
from aiive.mcp.bootstrap import MCPTool
from aiive.tools.base import BaseTool
from aiive.tools.shell_policy import classify_shell_command

ToolTrustMode = Literal["standard", "full_trust", "partial_trust"]

_FILE_MUTATIONS = frozenset({
    "safe_delete", "edit_file", "write_file", "apply_patch", "apply_patch_to_inactive_slot",
    "desktop_fs_write_text", "desktop_fs_write_binary", "desktop_fs_edit_text", "desktop_fs_mkdir",
    "desktop_fs_copy", "desktop_fs_move", "desktop_fs_delete",
})
_APPROVAL_TOOLS = frozenset({"run_selfdev_repair", "forget", "install_mcp_sandbox", "desktop_exec"})


class ToolIdentity(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    definition_source: str
    descriptor_hash: str
    executor_node_id: str
    mcp_executor_identity: str

    @override
    def __hash__(self) -> int:
        return hash((self.capability_id, self.definition_source, self.descriptor_hash,
                     self.executor_node_id, self.mcp_executor_identity))

    @classmethod
    def from_tool(cls, tool: BaseTool) -> ToolIdentity:
        return cls(
            capability_id=tool.safety.capability_id,
            definition_source=tool.safety.definition_source,
            descriptor_hash=tool.safety.descriptor_hash,
            executor_node_id=tool.executor_node_id,
            mcp_executor_identity=tool.execution_identity if isinstance(tool, MCPTool) else "",
        )


class ToolTrustPolicy(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    mode: ToolTrustMode = "standard"
    approval_exempt_capabilities: frozenset[ToolIdentity] = frozenset()

    @classmethod
    def load(cls, db: Session, *, lock_for_dispatch: bool = False) -> ToolTrustPolicy:
        # Read columns directly so a long-lived ORM session observes settings saved by another client.
        statement = select(
            ToolTrustSetting.mode, ToolTrustSetting.approval_exempt_capabilities,
        ).where(ToolTrustSetting.id == 1)
        if lock_for_dispatch:
            statement = statement.with_for_update(read=True)
        row = db.execute(statement).mappings().one_or_none()
        if row is None:
            return cls()
        return cls.model_validate(dict(row))

    @staticmethod
    def save(db: Session, mode: ToolTrustMode, exemptions: list[ToolIdentity] | None) -> None:
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        values: dict[str, Any] = {"mode": mode}
        if exemptions is not None:
            values["approval_exempt_capabilities"] = [item.model_dump() for item in exemptions]
        statement = insert(ToolTrustSetting).values(id=1, **values)
        db.execute(statement.on_conflict_do_update(index_elements=[ToolTrustSetting.id], set_=values))

    def approval_reason(self, tool: BaseTool, arguments: dict[str, Any]) -> str | None:
        if self.mode == "full_trust" or (
            self.mode == "partial_trust" and ToolIdentity.from_tool(tool) in self.approval_exempt_capabilities
        ):
            return None
        return self.standard_reason(tool, arguments)

    @staticmethod
    def standard_reason(tool: BaseTool, arguments: dict[str, Any]) -> str | None:
        safety = tool.safety
        capability_id = safety.capability_id
        if capability_id in {"promote_slot", "rollback_slot"} or (
            capability_id == "run_selfdev_repair" and arguments.get("promote") is True
        ):
            return "release_change_requires_approval"
        if capability_id in _FILE_MUTATIONS:
            return "file_mutation_requires_approval"
        if capability_id in _APPROVAL_TOOLS:
            return "tool_requires_approval"
        if safety.definition_source == "remote_mcp" and (safety.writes_external_world or safety.can_delete):
            return "remote_mcp_mutation_requires_approval"
        if capability_id == "run_shell":
            verdict = classify_shell_command(str(arguments.get("command") or ""))
            return None if verdict.read_only else f"shell_{verdict.reason}"
        if safety.requires_confirmation or safety.risk_level in {"high", "critical"}:
            return "high_risk_action_requires_approval"
        return None
