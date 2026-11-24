from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterable
from datetime import timezone
from pathlib import Path

from aiive.storage.data_paths import data_path, data_root
from aiive.task_runtime.worktree import WorktreeIsolation, _run_git

# Generated directories are omitted from snapshots, not from development operations.
GENERATED = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache",
             ".ruff_cache", ".mypy_cache", "dist", "build", "coverage"}
ROOT_STATE = {".data", "slots", "runtime", "pgdata"}
TERMINAL = {"succeeded", "failed", "partial", "cancelled", "expired"}
_last_scan = 0.0


def source_files(root: Path, *, git: bool = False) -> list[str]:
    if git:
        entries = _run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root, check=True)
        return sorted({name for name in entries.stdout.split("\0") if name and
                       ((root / name).is_file() or (root / name).is_symlink())})
    from aiive.project_paths import runtime_root, slots_root

    excluded_roots = {data_root(), slots_root(), runtime_root()}
    files = []
    for directory, dirs, names in os.walk(root):
        base = Path(directory)
        excluded = GENERATED | (ROOT_STATE if base == root else set())
        dirs[:] = [name for name in dirs if name not in excluded and (base / name) not in excluded_roots]
        files.extend((base / name).relative_to(root).as_posix() for name in names
                     if name not in GENERATED and not name.endswith((".pyc", ".pyo"))
                     and (name == ".env.example" or not name.startswith(".env"))
                     and ((base / name).is_file() or (base / name).is_symlink()))
    return sorted(files)


def copy_files(source: Path, target: Path, paths: list[str]) -> None:
    for name in paths:
        src = source / name
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            dst.unlink()
        shutil.copy2(src, dst, follow_symlinks=False)


def inventory(root: Path, *, git: bool = False, paths: Iterable[str] | None = None) -> dict[str, str]:
    result = {}
    for name in source_files(root, git=git) if paths is None else paths:
        path = root / name
        if path.is_symlink():
            result[name] = "link:" + os.readlink(path)
        else:
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            result[name] = f"{path.stat().st_mode & 0o111}:{digest}"
    return result


def workspace_stamp(workspace: Path) -> list[tuple[str, int, int]]:
    """Detect normal Shell edits without hashing the full worktree after every command."""
    files = source_files(workspace, git=(workspace / ".git").exists())
    entries = []
    for name in files:
        try:
            info = (workspace / name).lstat()
        except FileNotFoundError:
            continue
        entries.append((name, info.st_mtime_ns, info.st_size))
    return entries


def snapshot_hash(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def changes(workspace: Path, metadata: dict, current: dict[str, str] | None = None) -> list[str]:
    if current is None:
        current = inventory(workspace, git=metadata["git"])
    original = metadata["files"]
    return sorted(name for name in current.keys() | original.keys() if current.get(name) != original.get(name))


def metadata_path(workspace: Path) -> Path:
    return workspace.with_suffix(".json")


def read_metadata(workspace: Path) -> dict:
    return json.loads(metadata_path(workspace).read_text(encoding="utf-8"))


def create_workspace(task_id: str, canonical: Path, *, selfdev: bool = False) -> tuple[Path, dict]:
    workspace = data_path("task-worktrees", WorktreeIsolation._safe_task_id(task_id))
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if metadata_path(workspace).is_file() and workspace.is_dir():
        return workspace, read_metadata(workspace)
    try:
        isolation = WorktreeIsolation(canonical)
    except (ValueError, FileNotFoundError):
        isolation = None
    if isolation is not None:
        isolation.create(task_id)
    else:
        if workspace.exists():
            # A missing receipt means no task actions were dispatched from this copy.
            shutil.rmtree(workspace)
        workspace.mkdir()
        copy_files(canonical, workspace, source_files(canonical))
    deployment = {}
    if selfdev:
        from aiive.supervisor.slot_manager import SlotManager, tree_fingerprint

        manager = SlotManager()
        active = manager.active_app_root()
        slot = manager.get_active_slot()
        full_snapshot = manager._read_manifest(active.parent).get("source_snapshot") is True
        if (active / "backend" / "aiive").is_dir():
            active_files = source_files(active)
            if full_snapshot:
                for name in set(source_files(workspace, git=isolation is not None)) - set(active_files):
                    (workspace / name).unlink()
            copy_files(active, workspace, active_files)
            if isolation is not None and _run_git(["status", "--porcelain"], workspace, check=True).stdout:
                _run_git(["add", "-A"], workspace, check=True)
                _run_git(["-c", "user.name=AIive", "-c", "user.email=worker@aiive.local",
                          "commit", "-m", "Record deployed workspace baseline"], workspace, check=True)
        deployment = {"active_slot": slot, "active_fingerprint": tree_fingerprint(active)}
    base_commit = _run_git(["rev-parse", "HEAD"], workspace, check=True).stdout.strip() if isolation else None
    metadata = {"git": isolation is not None, "base_commit": base_commit,
                "source": str(canonical), "deployment": deployment,
                "files": inventory(workspace, git=isolation is not None)}
    state_file = metadata_path(workspace)
    temp = state_file.with_suffix(".tmp")
    temp.write_text(json.dumps(metadata), encoding="utf-8")
    os.replace(temp, state_file)
    return workspace, metadata


def _archive_changes(db, task, workspace: Path, metadata: dict) -> None:
    import io

    from aiive.db.models import TaskArtifact
    from aiive.storage.object_store import import_content_addressed
    from aiive.task_runtime.read_repository import TaskReadRepository, inherit_sources

    if not TaskReadRepository(db).task_visible(task):
        return
    if db.query(TaskArtifact.id).filter_by(task_id=task.id, artifact_type="workspace_delta").first():
        return
    changed = changes(workspace, metadata)
    if not changed:
        return
    with tempfile.NamedTemporaryFile(dir=workspace.parent, delete=False) as stream:
        archive = Path(stream.name)
        try:
            with tarfile.open(fileobj=stream, mode="w:gz") as tar:
                deleted = []
                for name in changed:
                    path = workspace / name
                    if path.is_file() or path.is_symlink():
                        tar.add(path, arcname=f"files/{name}", recursive=False)
                    else:
                        deleted.append(name)
                manifest = json.dumps({"deleted": deleted, "source": metadata["source"]}, ensure_ascii=False).encode()
                info = tarfile.TarInfo("changes.json")
                info.size = len(manifest)
                tar.addfile(info, io.BytesIO(manifest))
            stream.close()
            ref, digest, size = import_content_addressed("task-artifacts", archive)
            artifact = TaskArtifact(
                task_id=task.id, name="workspace-changes.tar.gz", artifact_type="workspace_delta",
                summary=f"{len(changed)} changed paths", object_bucket=ref.bucket, object_key=ref.key,
                content_hash=digest, content_size=size, mime_type="application/gzip",
            )
            db.add(artifact)
            db.flush()
            inherit_sources(db, "task_artifact", artifact.id, [("agent_task", task.id)])
            db.commit()
        finally:
            archive.unlink(missing_ok=True)


def _timestamp(value) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def reap_workspaces(db) -> None:
    global _last_scan
    now = time.time()
    if now - _last_scan < 60:
        return
    _last_scan = now
    from aiive.config import settings
    from aiive.db.models import AgentAction, AgentTask
    from aiive.task_runtime.repository import TaskRepository
    from aiive.task_runtime.schemas import TaskBudget
    from aiive.tools.shell_sessions import cancel_task_sessions, prune_sessions

    prune_sessions()
    cutoff = now - settings.aiive_coding_workspace_retention_days * 86400
    root = data_path("task-worktrees")
    for workspace in root.iterdir() if root.is_dir() else ():
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        task = db.get(AgentTask, workspace.name)
        try:
            if task is not None and task.status not in TERMINAL:
                budget = TaskBudget.from_stored(task.budgets)
                lifetime = budget.max_wall_time_seconds
                blocked = task.status in {"blocked_user", "blocked_approval", "blocked_node"}
                if blocked and lifetime and _timestamp(task.created_at) + lifetime < now:
                    TaskRepository(db).cancel_task(task.id, reason="workspace_budget_expired")
                    db.commit()
                continue
            if task is not None and db.query(AgentAction.id).filter(
                AgentAction.task_id == task.id, AgentAction.status.in_(["dispatched", "running"]),
            ).first():
                continue
            if cancel_task_sessions(workspace.name):
                continue
            age = task.updated_at if task is not None else None
            if (_timestamp(age) if age else workspace.stat().st_mtime) >= cutoff:
                continue
            metadata = read_metadata(workspace) if metadata_path(workspace).is_file() else None
            if task is not None and (metadata or (workspace / ".git").is_file()):
                _archive_changes(db, task, workspace, metadata or {
                    "git": True, "files": {}, "source": (task.task_state or {}).get("source_repository"),
                })
            repository = (metadata or {}).get("source") or ((task.task_state or {}).get("source_repository") if task else None)
            if repository and (workspace / ".git").is_file():
                WorktreeIsolation(Path(repository)).remove(workspace.name, force=True)
            else:
                shutil.rmtree(workspace)
            metadata_path(workspace).unlink(missing_ok=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            logging.getLogger(__name__).exception("Workspace cleanup failed: %s", workspace)
