"""Electron Desktop Node 查询、线程绑定和双向 WebSocket 端点。"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal, get_db
from aiive.config import settings
from aiive.desktop.connection_manager import desktop_connection_manager
from aiive.desktop.node_service import desktop_node_service
from aiive.desktop.protocol import DESKTOP_PROTOCOL_VERSION, DesktopActionAck, definition_for
from aiive.desktop.reconciliation import desktop_reconciliation_service
from aiive.desktop.schemas import (
    DesktopBindRequest,
    DesktopHello,
    DesktopOperationResult,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _node_payload(node: Any) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "name": node.name,
        "platform": node.platform,
        "arch": node.arch,
        "app_version": node.app_version,
        "capabilities": node.capabilities or [],
        "status": node.status,
        "connected": desktop_connection_manager.is_connected(node.id),
        "last_seen_at": node.last_seen_at,
        "lease_expires_at": node.lease_expires_at,
    }


@router.get("/api/desktop/nodes")
def list_desktop_nodes(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    nodes = desktop_node_service.list_nodes(db)
    db.commit()
    return [_node_payload(node) for node in nodes]


@router.post("/api/desktop/bind")
def bind_desktop_node(
    request: DesktopBindRequest, db: Session = Depends(get_db),
) -> dict[str, str]:
    try:
        binding = desktop_node_service.bind_thread(db, request.thread_id, request.node_id)
        db.commit()
    except ValueError as error:
        db.rollback()
        detail = str(error)
        raise HTTPException(
            status_code=404 if detail.endswith("not_found") else 409,
            detail=detail,
        ) from error
    return {"thread_id": binding.thread_id, "node_id": binding.node_id}


@router.websocket("/ws/desktop/{node_id}")
async def desktop_node_socket(websocket: WebSocket, node_id: str) -> None:
    await websocket.accept()
    registered = False
    try:
        raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        envelope = json.loads(raw_hello)
        if not isinstance(envelope, dict) or envelope.get("type") != "hello":
            await websocket.close(code=1008, reason="first message must be hello")
            return
        hello = DesktopHello.model_validate(envelope.get("data"))
        if hello.node_id != node_id:
            await websocket.close(code=1008, reason="node id mismatch")
            return
        if hello.protocol_version != DESKTOP_PROTOCOL_VERSION:
            await websocket.close(code=1008, reason="desktop protocol version mismatch")
            return
        if not hmac.compare_digest(hello.auth_token, settings.aiive_desktop_node_auth_token):
            await websocket.close(code=1008, reason="desktop node authentication failed")
            return
        unsupported = [cap.name for cap in hello.capabilities if definition_for(cap.name) is None]
        if unsupported:
            await websocket.close(code=1008, reason="unsupported desktop capabilities")
            return

        # 先发布当前连接代际，再提交 NodeOnline/wakeup。这样 Worker 被唤醒时
        # registry 一定能看到可投递连接，hello.ack 的 connected 也是真值。
        desktop_connection_manager.attach(node_id, websocket)
        registered = True
        db = SessionLocal()
        try:
            node = desktop_node_service.register_online(db, hello)
            from aiive.task_runtime.watchers import WatcherService

            WatcherService(db).wake_node_tasks(node_id)
            db.commit()
            payload = _node_payload(node)
        finally:
            db.close()
        await websocket.send_text(json.dumps({
            "type": "hello.ack",
            "protocol_version": DESKTOP_PROTOCOL_VERSION,
            "data": {**payload, "protocol_version": DESKTOP_PROTOCOL_VERSION},
        }, ensure_ascii=False, default=str))
        reconcile_db = SessionLocal()
        try:
            outstanding = desktop_reconciliation_service.outstanding_action_ids(reconcile_db, node_id)
        finally:
            reconcile_db.close()
        if outstanding:
            await websocket.send_text(json.dumps({
                "type": "journal.query",
                "protocol_version": DESKTOP_PROTOCOL_VERSION,
                "data": {"action_ids": outstanding},
            }))

        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            if not isinstance(message, dict):
                continue
            if not desktop_connection_manager.is_current_connection(node_id, websocket):
                await websocket.close(code=1008, reason="desktop connection superseded")
                return
            if message.get("protocol_version") != DESKTOP_PROTOCOL_VERSION:
                await websocket.close(code=1008, reason="desktop protocol version mismatch")
                return
            message_type = message.get("type")
            if message_type == "heartbeat":
                heartbeat_db = SessionLocal()
                try:
                    if not desktop_node_service.heartbeat(heartbeat_db, node_id):
                        await websocket.close(code=1008, reason="unknown node")
                        return
                    heartbeat_db.commit()
                finally:
                    heartbeat_db.close()
                await websocket.send_text(json.dumps({"type": "heartbeat.ack", "data": {}}))
            elif message_type == "operation.result":
                result = DesktopOperationResult.model_validate(message.get("data"))
                identity_fields = (
                    result.action_id, result.idempotency_key, result.arguments_hash,
                )
                accepted = not any(identity_fields)
                if all(identity_fields):
                    terminal_status = result.status or ("committed" if result.ok else "failed")
                    accepted = (
                        result.request_id == result.action_id
                        and terminal_status in {"committed", "failed", "unknown"}
                        and (terminal_status == "committed") == result.ok
                    )
                    result_db = SessionLocal()
                    try:
                        accepted = accepted and desktop_reconciliation_service.apply_entry(result_db, node_id, {
                            "action_id": result.action_id,
                            "idempotency_key": result.idempotency_key,
                            "arguments_hash": result.arguments_hash,
                            "status": terminal_status,
                            "result_hash": result.result_hash,
                            "result": result.result,
                            "error": result.error,
                        }, reconcile_action=False)
                        result_db.commit()
                    except Exception:
                        accepted = False
                        result_db.rollback()
                        logger.exception("Desktop Action result 投影失败: action_id=%s", result.action_id)
                    finally:
                        result_db.close()
                if accepted:
                    desktop_connection_manager.resolve_result(
                        result.request_id,
                        node_id=node_id,
                        action_id=result.action_id or None,
                        ok=result.ok,
                        result=result.result,
                        error=result.error,
                        idempotent_replay=result.idempotent_replay,
                    )
            elif message_type == "operation.ack":
                ack = DesktopActionAck.model_validate(message.get("data"))
                request_id = ack.request_id
                action_id = ack.action_id
                if request_id != action_id:
                    await websocket.close(code=1008, reason="desktop action ack identity mismatch")
                    return
                desktop_connection_manager.resolve_ack(request_id, action_id)
                ack_db = SessionLocal()
                try:
                    desktop_reconciliation_service.record_ack(ack_db, node_id, action_id, ack.status)
                    ack_db.commit()
                finally:
                    ack_db.close()
            elif message_type == "journal.snapshot":
                raw_data = message.get("data")
                data = raw_data if isinstance(raw_data, dict) else {}
                raw_entries = data.get("entries")
                entries = raw_entries if isinstance(raw_entries, list) else []
                raw_missing = data.get("missing_action_ids")
                missing = raw_missing if isinstance(raw_missing, list) else []
                journal_db = SessionLocal()
                try:
                    for entry in entries[:500]:
                        if isinstance(entry, dict):
                            desktop_reconciliation_service.apply_entry(journal_db, node_id, entry)
                    for action_id in missing[:500]:
                        if isinstance(action_id, str):
                            desktop_reconciliation_service.apply_missing(journal_db, node_id, action_id)
                    journal_db.commit()
                except Exception:
                    journal_db.rollback()
                    logger.exception("Desktop journal 对账失败: node_id=%s", node_id)
                finally:
                    journal_db.close()
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except (json.JSONDecodeError, ValidationError):
        logger.warning("Desktop Node 消息格式无效: node_id=%s", node_id, exc_info=True)
        try:
            await websocket.close(code=1008, reason="invalid desktop protocol message")
        except Exception:
            pass
    except Exception:
        logger.exception("Desktop Node WebSocket 异常: node_id=%s", node_id)
    finally:
        if registered and desktop_connection_manager.detach(node_id, websocket):
            offline_db = SessionLocal()
            try:
                desktop_node_service.mark_offline(offline_db, node_id)
                offline_db.commit()
            finally:
                offline_db.close()
