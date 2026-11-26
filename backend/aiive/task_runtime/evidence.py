"""Action 结果的有界摘要、Evidence/Artifact 对象存储。"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentAction, TaskArtifact, TaskEvidence
from aiive.runtime.tool_output_guard import guard_tool_output
from aiive.storage.object_store import ObjectRef, get_range, import_content_addressed, put_content_addressed


INLINE_LIMIT = 4096
SUMMARY_LIMIT = 1200


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _summary(
    tool_name: str,
    value: Any,
    *,
    thread_id: str = "",
    trace_id: str = "",
    tool_call_id: str = "",
) -> str:
    """Evidence 摘要复用统一回填守卫。

    后台任务的工具结果只有这份摘要会再次进入 LLM 上下文，因此必须走同一个入口：
    小结果原样保留；大结果落盘并给出总量、日志路径与续读位置，且保持 JSON 结构完整，
    而不是在字符预算处把 JSON 从中间切断。
    """
    text = json.dumps(value, ensure_ascii=False, default=str)
    limit = 32000 if tool_name in FULL_OBSERVATION_TOOLS else SUMMARY_LIMIT
    return guard_tool_output(
        tool_name,
        text,
        thread_id=thread_id,
        trace_id=trace_id,
        tool_call_id=tool_call_id,
        max_chars=limit,
    ).content


FULL_OBSERVATION_TOOLS = frozenset({
    "read_file", "shell_session", "run_selfdev_repair",
    "read_attachment", "list_thread_attachments", "record_attachment_progress",
    "list_task_files", "read_task_artifact", "read_task_evidence",
    "read_document", "read_knowledge_chunk",
    "fetch_web_page", "get_runtime_identity",
})


class EvidenceStore:
    def capture_result(
        self,
        db: Session,
        *,
        task_id: str,
        run_id: str,
        action: AgentAction,
        result: Any,
        kind: str = "tool_result",
    ) -> TaskEvidence:
        from aiive.db.models import AgentTask
        from aiive.forget.concurrency import acquire_forget_shared_lock
        from aiive.forget.durable_sources import collect_sources, references_visible
        acquire_forget_shared_lock(db)
        task = db.get(AgentTask, task_id)
        from aiive.task_runtime.read_repository import TaskReadRepository, TaskContentUnavailable
        if task is not None and not TaskReadRepository(db).task_visible(task):
            raise TaskContentUnavailable("task_unavailable")
        origin = {"source_event_id": getattr(task, "source_event_id", None),
                  "source_turn_record_id": getattr(task, "source_turn_record_id", None)} if task else {}
        dependencies = set()
        for payload in (origin, action.arguments, result):
            collect_sources(payload, dependencies)
        grouped = {}
        for source_type, source_id in dependencies:
            grouped.setdefault(source_type, set()).add(source_id)
        if not references_visible(db, grouped):
            raise ValueError("forget_source_blocked")
        result = result if isinstance(result, dict) else {"value": result}
        if action.capability_id in {"run_shell", "shell_session"}:
            result = self._capture_shell_logs(db, task_id, action, result)
        data = _json_bytes(result)
        digest = hashlib.sha256(data).hexdigest()
        inline: dict[str, Any] | None
        bucket: str | None = None
        key: str | None = None
        if len(data) > INLINE_LIMIT:
            ref, digest = put_content_addressed(
                "task-evidence", data,
                {"task_id": task_id, "run_id": run_id, "action_id": action.id},
            )
            bucket, key = ref.bucket, ref.key
            inline = None
        else:
            inline = result
        evidence = TaskEvidence(
            task_id=task_id,
            run_id=run_id,
            action_id=action.id,
            kind=kind,
            summary=_summary(
                action.capability_id,
                result,
                thread_id=task_id,
                trace_id=run_id,
                tool_call_id=action.id,
            ),
            inline_payload=inline,
            object_bucket=bucket,
            object_key=key,
            content_hash=digest,
            content_size=len(data),
            mime_type="application/json",
        )
        db.add(evidence)
        db.flush()
        from aiive.forget.durable_sources import persist_sources
        persist_sources(db, "task_evidence", evidence.id, origin, action.arguments, result)
        from aiive.task_runtime.read_repository import inherit_sources
        inherit_sources(db, "task_evidence", evidence.id, [("agent_task", task_id), ("goal_plan", task_id), ("agent_action", action.id)])
        inherit_sources(db, "agent_action", action.id, [("task_evidence", evidence.id)])
        refs = list(action.evidence_refs or [])
        refs.append({"evidence_id": evidence.id, "kind": kind, "content_hash": digest})
        action.evidence_refs = refs
        summary: dict[str, Any] = {
            "ok": bool(result.get("ok", True)),
            "summary": evidence.summary,
            "evidence_id": evidence.id,
            "content_size": evidence.content_size,
        }
        shell_result = result.get("result", result.get("details", result))
        if action.capability_id in {"run_shell", "shell_session"} and isinstance(shell_result, dict) and shell_result.get("output_artifacts"):
            summary["output_artifacts"] = shell_result["output_artifacts"]
        if action.capability_id in {"promote_slot", "run_selfdev_repair"} and isinstance(shell_result, dict):
            promotion = shell_result.get("promotion") if action.capability_id == "run_selfdev_repair" else shell_result
            if isinstance(promotion, dict):
                if promotion.get("status") == "switch_requested" and promotion.get("request_id"):
                    summary["promotion_request_id"] = promotion["request_id"]
                elif promotion.get("ok") and promotion.get("release_status") in {"monitoring", "restart_required"}:
                    summary["deployment_confirmation_required"] = True
        action.result_summary = summary
        self._capture_declared_artifacts(db, task_id, action, result)
        db.flush()
        return evidence

    @staticmethod
    def _capture_shell_logs(
        db: Session, task_id: str, action: AgentAction, result: dict[str, Any],
    ) -> dict[str, Any]:
        from pathlib import Path

        from aiive.runtime.tool_output_store import tool_output_root
        from aiive.task_runtime.read_repository import inherit_sources

        key = "result" if isinstance(result.get("result"), dict) else "details" if isinstance(result.get("details"), dict) else None
        payload = dict(result[key] if key else result)
        artifacts = {}
        for stream in ("stdout", "stderr"):
            raw_path = payload.pop(f"{stream}_log", None)
            if not raw_path:
                continue
            try:
                path = Path(raw_path)
                # Only output paths issued by this built-in executor are eligible.
                if path.is_symlink() or path.resolve().parent != tool_output_root().resolve() or path.suffix != ".log":
                    raise ValueError("unexpected Shell log path")
                ref, digest, size = import_content_addressed("task-artifacts", path)
            except (ValueError, OSError, RuntimeError):
                artifacts[stream] = {"available": False}
                continue
            if size == 0:
                continue
            artifact = TaskArtifact(
                task_id=task_id, action_id=action.id, name=f"Shell {stream}.log",
                artifact_type="shell_log", summary=f"Shell {stream}",
                object_bucket=ref.bucket, object_key=ref.key, content_hash=digest,
                mime_type="text/plain; charset=utf-8", content_size=size,
            )
            db.add(artifact)
            db.flush()
            inherit_sources(db, "task_artifact", artifact.id, [
                ("agent_task", task_id), ("goal_plan", task_id), ("agent_action", action.id),
            ])
            truncated = payload.get("capture_truncated") or {}
            artifacts[stream] = {"artifact_id": artifact.id, "size": size, "truncated": bool(truncated.get(stream))}
        if artifacts:
            payload["output_artifacts"] = artifacts
        return {**result, key: payload} if key else payload

    def _capture_declared_artifacts(
        self, db: Session, task_id: str, action: AgentAction, result: Any,
    ) -> list[TaskArtifact]:
        if not isinstance(result, dict):
            return []
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        raw_artifacts = inner.get("artifacts") if isinstance(inner, dict) else None
        if not isinstance(raw_artifacts, list):
            return []
        captured: list[TaskArtifact] = []
        for index, raw in enumerate(raw_artifacts[:32]):
            if not isinstance(raw, dict):
                continue
            mime = str(raw.get("mime_type") or "application/octet-stream")
            if isinstance(raw.get("content_base64"), str):
                try:
                    data = base64.b64decode(raw["content_base64"], validate=True)
                except Exception:
                    continue
            elif isinstance(raw.get("content"), str):
                data = raw["content"].encode("utf-8")
                if mime == "application/octet-stream":
                    mime = "text/plain; charset=utf-8"
            else:
                continue
            ref, digest = put_content_addressed(
                "task-artifacts", data,
                {"task_id": task_id, "action_id": action.id},
            )
            artifact = TaskArtifact(
                task_id=task_id,
                action_id=action.id,
                name=str(raw.get("name") or f"artifact-{index + 1}")[:255],
                artifact_type=str(raw.get("artifact_type") or "file")[:64],
                summary=str(raw.get("summary") or ""),
                object_bucket=ref.bucket,
                object_key=ref.key,
                content_hash=digest,
                mime_type=mime[:255],
                content_size=len(data),
            )
            db.add(artifact)
            db.flush()
            from aiive.task_runtime.read_repository import inherit_sources
            inherit_sources(db, "task_artifact", artifact.id, [("agent_task", task_id), ("goal_plan", task_id), ("agent_action", action.id)])
            captured.append(artifact)
        return captured

    @staticmethod
    def read(db: Session, evidence: TaskEvidence, offset: int, max_bytes: int) -> tuple[bytes, bool]:
        from aiive.task_runtime.read_repository import TaskReadRepository, TaskContentUnavailable
        if not TaskReadRepository(db).visible_child("task_evidence", evidence):
            raise TaskContentUnavailable("evidence_unavailable")
        if evidence.object_bucket and evidence.object_key:
            data = get_range(
                ObjectRef(evidence.object_bucket, evidence.object_key),
                offset=offset,
                max_bytes=max_bytes,
            )
            return data, offset + len(data) < evidence.content_size
        raw = _json_bytes(evidence.inline_payload or {})
        return raw[offset:offset + max_bytes], offset + max_bytes < len(raw)


class ArtifactStore:
    @staticmethod
    def read(db: Session, artifact: TaskArtifact, offset: int, max_bytes: int) -> tuple[bytes, bool]:
        from aiive.task_runtime.read_repository import TaskReadRepository, TaskContentUnavailable
        if not TaskReadRepository(db).visible_child("task_artifact", artifact):
            raise TaskContentUnavailable("artifact_unavailable")
        data = get_range(ObjectRef(artifact.object_bucket, artifact.object_key), offset=offset, max_bytes=max_bytes)
        return data, offset + len(data) < artifact.content_size


def artifact_bytes(db: Session, artifact: TaskArtifact, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    """Compatibility wrapper; a Session and authorization are mandatory."""
    return ArtifactStore.read(db, artifact, offset, max_bytes)
