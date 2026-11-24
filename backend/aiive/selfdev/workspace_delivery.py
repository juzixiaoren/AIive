from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from aiive.selfdev.repair_guard import CandidateSlotGuard, default_repair_lock
from aiive.supervisor.slot_manager import create_version_manifest, tree_fingerprint
from aiive.task_runtime.workspace import changes, copy_files, inventory, read_metadata, snapshot_hash, source_files
from aiive.task_runtime.worktree import _run_git
from aiive.tools.shell_sessions import task_has_live_sessions


def deliver_workspace(workspace: Path, *, task_id: str, run_id: str, files: list[str] | None = None) -> dict:
    """Validate a frozen copy of the task's cumulative changes under the existing release lock."""
    from aiive.eval.gate import evaluate_promotion_gate
    from aiive.selfdev.targeted_test_runner import TargetedTestRunner

    if task_has_live_sessions(task_id):
        return {"ok": False, "error": "wait_for_workspace_shell_sessions"}
    metadata = read_metadata(workspace)
    before = inventory(workspace, git=metadata["git"])
    changed = changes(workspace, metadata, before)
    if not changed:
        return {"ok": False, "error": "workspace_has_no_changes"}
    guard = CandidateSlotGuard()
    manager = guard.manager
    lock = default_repair_lock(manager)
    handle = lock.acquire(run_id=run_id)
    if handle is None:
        return {"ok": False, "error": "concurrent_repair_in_progress"}
    try:
        active = manager.get_active_slot()
        baseline = metadata["deployment"]
        if not baseline or baseline != {
            "active_slot": active, "active_fingerprint": tree_fingerprint(manager.active_app_root()),
        }:
            return {"ok": False, "error": "workspace_deployment_baseline_changed"}
        candidate = guard.candidate_slot()
        if candidate is None or not manager.has_local_slot_paths(candidate.name):
            return {"ok": False, "error": "invalid_inactive_slot_path"}
        target = candidate.root / "app"
        candidate.root.mkdir(parents=True, exist_ok=True)
        verification_files = sorted(set(changed) | set(files or []))
        # Verify the candidate bytes; no generated dependencies or git metadata are deployed.
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=candidate.root) as staging:
            staged = Path(staging) / "app"
            staged.mkdir()
            copy_files(workspace, staged, source_files(workspace, git=metadata["git"]))
            if inventory(staged, paths=before) != before:
                return {"ok": False, "error": "workspace_changed_during_copy"}
            fingerprint = tree_fingerprint(staged)
            if guard.manager.get_active_slot() != active:
                return {"ok": False, "error": "workspace_deployment_baseline_changed"}
            guard.mark_not_promotable(candidate.name, reason="workspace_validation_in_progress", run_id=run_id)
            if target.exists():
                shutil.rmtree(target)
            staged.replace(target)
        report = TargetedTestRunner(repo_root=target).run_for_changed_files(verification_files)
        output = {"candidate_root": str(target), "candidate_slot": candidate.name, "run_id": run_id,
                  "changed_files": changed, "workspace_hash": snapshot_hash(before), "targeted_test_report": report}
        if not report.get("ok"):
            if metadata.get("base_commit"):
                diff = _run_git(["diff", "--no-ext-diff", metadata["base_commit"], "--"], workspace, check=False)
                if diff.returncode == 0:
                    output["git_diff"] = diff.stdout[:8000]
                    output["git_diff_truncated"] = len(diff.stdout) > 8000
            guard.mark_not_promotable(candidate.name, reason="targeted_tests_failed", run_id=run_id)
            return {**output, "ok": False, "error": "targeted_tests_failed"}
        gate = evaluate_promotion_gate(candidate.root)
        output["gate"] = gate.to_summary()
        if not gate.ok:
            guard.mark_not_promotable(candidate.name, reason="safety_gate_failed", run_id=run_id)
            return {**output, "ok": False, "error": "safety_gate_failed"}
        if (tree_fingerprint(target) != fingerprint or manager.get_active_slot() != active
                or tree_fingerprint(manager.active_app_root()) != baseline["active_fingerprint"]):
            return {**output, "ok": False, "error": "candidate_or_baseline_changed_during_validation"}
        from aiive.prompts.registry import PromptRegistry

        prompts = PromptRegistry(target / "backend" / "aiive" / "prompts")
        manifest = create_version_manifest(candidate.name)
        manifest["prompt_catalog_sha256"] = prompts.catalog_sha256
        manifest["stable_contract_audit_ref"] = prompts.render("agent.stable_contract").audit_ref
        manifest.update(source_snapshot=True, workspace_task_id=task_id, workspace_action_id=run_id)
        from aiive.supervisor.control import write_json_atomic

        write_json_atomic(candidate.root / "version_manifest.json", manifest)
        guard.mark_promotable(candidate.name, run_id=run_id, fingerprint=fingerprint, gate_ok=True)
        return {**output, "ok": True, "status": "tests_passed", "fingerprint": fingerprint}
    finally:
        lock.release(handle)
