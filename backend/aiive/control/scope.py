"""Task capability/path/network/secret scope 的确定性校验。"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


PATH_ARGUMENTS = frozenset({"path", "source", "destination", "cwd", "file_path", "target_file"})
URL_ARGUMENTS = frozenset({"url"})


def _normalize_remote_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    is_unc = raw.startswith("//")
    raw = re.sub(r"/+", "/", raw)
    if is_unc:
        raw = "/" + raw
    raw = posixpath.normpath(raw)
    if raw == ".":
        return ""
    if len(raw) > 3:
        raw = raw.rstrip("/")
    return raw.casefold() if re.match(r"^[a-zA-Z]:/", raw) or raw.startswith("//") else raw


def path_within(candidate: str, root: str) -> bool:
    candidate_n = _normalize_remote_path(candidate)
    root_n = _normalize_remote_path(root)
    if not candidate_n or not root_n:
        return False
    return candidate_n == root_n or candidate_n.startswith(root_n.rstrip("/") + "/")


@dataclass(frozen=True)
class TaskScope:
    allowed_capabilities: frozenset[str]
    allowed_roots: tuple[str, ...]
    allowed_hosts: frozenset[str]
    allow_network: bool = False
    allow_secrets: bool = False
    isolation_mode: str = "shared"

    @classmethod
    def from_brief(cls, brief: dict[str, Any]) -> "TaskScope":
        raw = brief.get("scope")
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            allowed_capabilities=frozenset(str(v) for v in raw.get("allowed_capabilities", []) if v),
            allowed_roots=tuple(str(v) for v in raw.get("allowed_roots", []) if v),
            allowed_hosts=frozenset(str(v).casefold() for v in raw.get("allowed_hosts", []) if v),
            allow_network=bool(raw.get("allow_network", False)),
            allow_secrets=bool(raw.get("allow_secrets", False)),
            isolation_mode=str(raw.get("isolation_mode", "shared")),
        )

    def allows_capability(self, capability_id: str) -> bool:
        return capability_id in self.allowed_capabilities

    def validate_arguments(self, capability_id: str, arguments: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        if not self.allows_capability(capability_id):
            issues.append(f"capability_not_in_task_scope:{capability_id}")
        for name, value in arguments.items():
            if name not in PATH_ARGUMENTS or not isinstance(value, str) or not value:
                continue
            if not self.allowed_roots:
                issues.append(f"path_scope_missing:{name}")
            elif not any(path_within(value, root) for root in self.allowed_roots):
                issues.append(f"path_outside_task_scope:{name}")
        for name, value in arguments.items():
            if name not in URL_ARGUMENTS or not isinstance(value, str) or not value:
                continue
            try:
                parsed = urlsplit(value)
                host = (parsed.hostname or "").casefold()
            except ValueError:
                issues.append(f"invalid_network_url:{name}")
                continue
            if parsed.scheme not in {"http", "https"} or not host:
                issues.append(f"invalid_network_url:{name}")
            elif self.allowed_hosts and not any(
                host == allowed or host.endswith("." + allowed)
                for allowed in self.allowed_hosts
            ):
                issues.append(f"host_outside_task_scope:{name}")
        if capability_id == "desktop_exec":
            cwd = arguments.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                issues.append("desktop_exec_requires_scoped_cwd")
            if arguments.get("env") and not self.allow_secrets:
                issues.append("secret_environment_not_allowed")
        return issues

    def as_envelope(self) -> dict[str, Any]:
        return {
            "allowed_capabilities": sorted(self.allowed_capabilities),
            "allowed_roots": list(self.allowed_roots),
            "allowed_hosts": sorted(self.allowed_hosts),
            "allow_network": self.allow_network,
            "allow_secrets": self.allow_secrets,
            "isolation_mode": self.isolation_mode,
        }


def action_resource_keys(
    capability_id: str,
    arguments: dict[str, Any],
    node_id: str | None,
    *,
    mutates_resources: bool = False,
) -> list[str]:
    prefix = f"node:{node_id}:" if node_id else "server:"
    keys: set[str] = set()
    if mutates_resources:
        keys.update({
            prefix + "path:" + _normalize_remote_path(str(value))
            for name, value in arguments.items()
            if name in PATH_ARGUMENTS and isinstance(value, str) and value
        })
    if any(marker in capability_id for marker in ("gui", "foreground", "screen", "keyboard", "mouse")):
        keys.add(prefix + "gui:foreground")
    if capability_id.startswith("selfdev_") or "slot" in capability_id:
        keys.add("server:selfdev:lifecycle")
    return sorted(keys)
