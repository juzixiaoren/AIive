"""Action 结果的有界摘要、Evidence/Artifact 对象存储。"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import AgentAction, TaskArtifact, TaskEvidence
from aiive.storage.object_store import ObjectRef, get_range, put_content_addressed


INLINE_LIMIT = 4096
SUMMARY_LIMIT = 1200


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def _summary(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[:SUMMARY_LIMIT] + f"… [truncated, chars={len(text)}]"


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
            inline = result if isinstance(result, dict) else {"value": result}
        evidence = TaskEvidence(
            task_id=task_id,
            run_id=run_id,
            action_id=action.id,
            kind=kind,
            summary=_summary(result),
            inline_payload=inline,
            object_bucket=bucket,
            object_key=key,
            content_hash=digest,
            content_size=len(data),
            mime_type="application/json",
        )
        db.add(evidence)
        db.flush()
        refs = list(action.evidence_refs or [])
        refs.append({"evidence_id": evidence.id, "kind": kind, "content_hash": digest})
        action.evidence_refs = refs
        action.result_summary = {
            "ok": bool(result.get("ok")) if isinstance(result, dict) and "ok" in result else True,
            "summary": evidence.summary,
            "evidence_id": evidence.id,
            "content_size": evidence.content_size,
        }
        self._capture_declared_artifacts(db, task_id, action, result)
        db.flush()
        return evidence

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
            captured.append(artifact)
        return captured

    @staticmethod
    def read(db: Session, evidence: TaskEvidence, offset: int, max_bytes: int) -> tuple[bytes, bool]:
        if evidence.object_bucket and evidence.object_key:
            data = get_range(
                ObjectRef(evidence.object_bucket, evidence.object_key),
                offset=offset,
                max_bytes=max_bytes,
            )
            return data, offset + len(data) < evidence.content_size
        raw = _json_bytes(evidence.inline_payload or {})
        return raw[offset:offset + max_bytes], offset + max_bytes < len(raw)


def artifact_bytes(artifact: TaskArtifact, offset: int, max_bytes: int) -> tuple[bytes, bool]:
    data = get_range(
        ObjectRef(artifact.object_bucket, artifact.object_key),
        offset=offset,
        max_bytes=max_bytes,
    )
    return data, offset + len(data) < artifact.content_size
