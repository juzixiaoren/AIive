import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class SafeDeleteScopeRegistry:
    def __init__(self):
        self._scopes: dict[str, Path] = {}

    def register(self, scope_id: str, root: Path) -> None:
        self._scopes[scope_id] = root.resolve()

    def get_scope_root(self, scope_id: str) -> Path | None:
        return self._scopes.get(scope_id)

    def list_scopes(self) -> dict[str, str]:
        return {k: str(v) for k, v in self._scopes.items()}


# Built-in scopes
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


def _build_default_registry() -> SafeDeleteScopeRegistry:
    registry = SafeDeleteScopeRegistry()
    registry.register("test_sandbox", REPO_ROOT / "tests" / "artifacts")
    registry.register("test_artifacts", REPO_ROOT / "tests" / "artifacts")
    registry.register("inactive_slot_placeholder", REPO_ROOT / "tests" / "artifacts" / "slot_placeholder")
    return registry


# Global singleton
_default_registry: SafeDeleteScopeRegistry | None = None


def get_scope_registry() -> SafeDeleteScopeRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = _build_default_registry()
    return _default_registry


DANGEROUS_PATHS = frozenset({
    "/",
    str(Path.home()),
    str(REPO_ROOT),
    str(REPO_ROOT / "backend"),
})


@dataclass
class DeleteDecision:
    allowed: bool
    reason: str = ""
    resolved_path: str = ""
    scope_id: str = ""
    mode: str = ""


def safe_delete(
    path: str,
    scope_id: str,
    mode: str = "trash",
    registry: SafeDeleteScopeRegistry | None = None,
) -> DeleteDecision:
    if registry is None:
        registry = get_scope_registry()

    # Symlink check before resolve
    raw_path = Path(path)
    if raw_path.is_symlink():
        return DeleteDecision(
            allowed=False,
            reason="Symlinks are not allowed for deletion",
            resolved_path=str(raw_path),
        )

    # Resolve
    try:
        resolved = raw_path.resolve()
    except Exception:
        return DeleteDecision(
            allowed=False,
            reason="Cannot resolve path",
            resolved_path=path,
        )

    resolved_str = str(resolved)

    # Deny dangerous paths
    for dangerous in DANGEROUS_PATHS:
        dangerous_resolved = str(Path(dangerous).resolve())
        if resolved_str == dangerous_resolved or resolved_str.startswith(
            dangerous_resolved + os.sep
        ):
            return DeleteDecision(
                allowed=False,
                reason=f"Path is within dangerous area: {dangerous}",
                resolved_path=resolved_str,
            )

    # Check scope
    scope_root = registry.get_scope_root(scope_id)
    if scope_root is None:
        return DeleteDecision(
            allowed=False,
            reason=f"Unknown scope: {scope_id}",
            resolved_path=resolved_str,
        )

    if not resolved_str.startswith(str(scope_root)):
        return DeleteDecision(
            allowed=False,
            reason=f"Path outside scope '{scope_id}': {scope_root}",
            resolved_path=resolved_str,
            scope_id=scope_id,
        )

    # Execute
    if not resolved.exists():
        return DeleteDecision(
            allowed=False,
            reason="Path does not exist",
            resolved_path=resolved_str,
            scope_id=scope_id,
            mode=mode,
        )

    try:
        if mode == "trash":
            trash_dir = resolved.parent / ".trash"
            trash_dir.mkdir(exist_ok=True)
            dest = trash_dir / resolved.name
            if dest.exists():
                dest = trash_dir / f"{resolved.name}.{_timestamp()}"
            shutil.move(str(resolved), str(dest))
        elif mode == "quarantine":
            quarantine_dir = resolved.parent / ".quarantine"
            quarantine_dir.mkdir(exist_ok=True)
            dest = quarantine_dir / resolved.name
            if dest.exists():
                dest = quarantine_dir / f"{resolved.name}.{_timestamp()}"
            shutil.move(str(resolved), str(dest))
        elif mode == "hard_delete_for_test_only":
            if resolved.is_dir():
                shutil.rmtree(str(resolved))
            else:
                resolved.unlink()
        else:
            return DeleteDecision(
                allowed=False,
                reason=f"Unknown mode: {mode}",
                resolved_path=resolved_str,
            )

        return DeleteDecision(
            allowed=True,
            reason=f"Deleted with mode={mode}",
            resolved_path=resolved_str,
            scope_id=scope_id,
            mode=mode,
        )
    except Exception as e:
        return DeleteDecision(
            allowed=False,
            reason=f"Delete failed: {e}",
            resolved_path=resolved_str,
        )


def _timestamp() -> str:
    import time
    return str(int(time.time()))
