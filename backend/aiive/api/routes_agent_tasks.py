"""Persistent Agent Task REST/WS API；旧 `/api/tasks` 提醒接口保持兼容。"""
from __future__ import annotations

import asyncio
import json
from pathlib import PurePath
from typing import Any, ClassVar
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal, get_db
from aiive.db.models import AgentTask, AgentTaskEvent, TaskArtifact, TaskEvidence
from aiive.task_runtime.evidence import EvidenceStore, artifact_bytes
from aiive.task_runtime.repository import TaskRepository
from aiive.task_runtime.router import normalize_task_brief
from aiive.task_runtime.schemas import TaskBudget, TaskBrief


router = APIRouter(prefix="/api/agent-tasks", tags=["agent-tasks"])


class CreateAgentTaskRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=36)
    goal: str = Field(min_length=1, max_length=20_000)
    title: str = Field(default="", max_length=255)
    task_type: str = Field(default="general", max_length=64)
    source_turn_record_id: str | None = None
    target_node_id: str | None = None
    task_brief: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=50, ge=0, le=100)


class TaskInputRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=20_000)


@router.post("")
def create_agent_task(request: CreateAgentTaskRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        brief = normalize_task_brief(request.task_type, request.task_brief)
        brief["goal"] = request.goal
        TaskBrief.model_validate(brief)
        TaskBudget.model_validate(request.budgets)
        task = TaskRepository(db).create_task(
            thread_id=request.thread_id,
            source_turn_record_id=request.source_turn_record_id,
            goal=request.goal,
            title=request.title or None,
            task_type=request.task_type,
            target_node_id=request.target_node_id,
            task_brief=brief,
            budgets=request.budgets,
            priority=request.priority,
        )
        db.commit()
        return TaskRepository(db).detail(task, include_events=True)
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("")
def list_agent_tasks(
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentTask)
    if thread_id:
        query = query.filter(AgentTask.thread_id == thread_id)
    if status:
        query = query.filter(AgentTask.status == status)
    tasks = query.order_by(AgentTask.updated_at.desc()).limit(limit).all()
    repo = TaskRepository(db)
    return [repo.summary(task) for task in tasks]


@router.get("/{task_id}")
def get_agent_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = TaskRepository(db).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return TaskRepository(db).detail(task, include_events=True)


@router.get("/{task_id}/events")
def get_agent_task_events(
    task_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if TaskRepository(db).get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    events = db.query(AgentTaskEvent).filter(
        AgentTaskEvent.task_id == task_id,
        AgentTaskEvent.sequence > after_sequence,
    ).order_by(AgentTaskEvent.sequence.asc()).limit(limit).all()
    return [
        {
            "id": item.id, "sequence": item.sequence, "event_type": item.event_type,
            "visibility": item.visibility, "run_id": item.run_id, "action_id": item.action_id,
            "payload": item.payload or {}, "created_at": item.created_at,
        }
        for item in events
    ]


@router.post("/{task_id}/cancel")
def cancel_agent_task(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        task = TaskRepository(db).cancel_task(task_id)
        db.commit()
        return {"task_id": task.id, "status": task.status}
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/{task_id}/input")
def send_agent_task_input(
    task_id: str, request: TaskInputRequest, db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        task = TaskRepository(db).send_input(task_id, request.content)
        db.commit()
        return {"task_id": task.id, "status": task.status, "input_accepted": True}
    except ValueError as error:
        db.rollback()
        status = 404 if str(error) == "task_not_found" else 409
        raise HTTPException(status_code=status, detail=str(error)) from error


@router.get("/{task_id}/evidence/{evidence_id}")
def read_task_evidence(
    task_id: str,
    evidence_id: str,
    offset: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=65536, ge=1, le=1_048_576),
    db: Session = Depends(get_db),
) -> Response:
    evidence = db.get(TaskEvidence, evidence_id)
    if evidence is None or evidence.task_id != task_id:
        raise HTTPException(status_code=404, detail="Evidence 不存在")
    data, truncated = EvidenceStore.read(db, evidence, offset, max_bytes)
    return Response(
        content=data,
        media_type=evidence.mime_type,
        headers={
            "X-AIive-Content-Hash": evidence.content_hash,
            "X-AIive-Truncated": "true" if truncated else "false",
            "X-AIive-Next-Offset": str(offset + len(data)),
        },
    )


@router.get("/{task_id}/artifacts/{artifact_id}")
def read_task_artifact(
    task_id: str,
    artifact_id: str,
    offset: int = Query(default=0, ge=0),
    max_bytes: int = Query(default=1_048_576, ge=1, le=1_048_576),
    db: Session = Depends(get_db),
) -> Response:
    artifact = db.get(TaskArtifact, artifact_id)
    if artifact is None or artifact.task_id != task_id:
        raise HTTPException(status_code=404, detail="Artifact 不存在")
    data, truncated = artifact_bytes(artifact, offset, max_bytes)
    # Header 只使用 ASCII fallback + RFC 5987 编码，拒绝文件名中的路径和换行语义。
    raw_name = PurePath(artifact.name.replace("\\", "/")).name
    safe_name = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "_" for char in raw_name)
    safe_name = safe_name.strip(".")[:120] or "artifact"
    encoded_name = quote(raw_name or "artifact", safe="")
    return Response(
        content=data,
        media_type=artifact.mime_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{encoded_name}'
            ),
            "X-AIive-Content-Hash": artifact.content_hash,
            "X-AIive-Truncated": "true" if truncated else "false",
            "X-AIive-Next-Offset": str(offset + len(data)),
        },
    )


@router.websocket("/ws/{task_id}")
async def agent_task_event_socket(websocket: WebSocket, task_id: str) -> None:
    """轻量事件流；断线后客户端使用 sequence 从 REST 补齐。"""
    await websocket.accept()
    raw_cursor = websocket.query_params.get("after_sequence", "0")
    cursor: int | None
    if raw_cursor == "latest":
        cursor = None
    else:
        try:
            cursor = max(0, int(raw_cursor))
        except ValueError:
            cursor = 0
    try:
        while True:
            db = SessionLocal()
            try:
                if db.get(AgentTask, task_id) is None:
                    await websocket.close(code=1008, reason="task not found")
                    return
                if cursor is None:
                    latest = db.query(AgentTaskEvent.sequence).filter(
                        AgentTaskEvent.task_id == task_id,
                    ).order_by(AgentTaskEvent.sequence.desc()).first()
                    cursor = int(latest[0]) if latest else 0
                events = db.query(AgentTaskEvent).filter(
                    AgentTaskEvent.task_id == task_id,
                    AgentTaskEvent.sequence > cursor,
                ).order_by(AgentTaskEvent.sequence.asc()).limit(200).all()
                for item in events:
                    cursor = int(item.sequence)
                    await websocket.send_text(json.dumps({
                        "type": "task_event",
                        "data": {
                            "id": item.id, "sequence": item.sequence,
                            "event_type": item.event_type, "visibility": item.visibility,
                            "run_id": item.run_id, "action_id": item.action_id,
                            "payload": item.payload or {}, "created_at": str(item.created_at),
                        },
                    }, ensure_ascii=False))
            finally:
                db.close()
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
