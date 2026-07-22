"""
运行时层 - 线程状态管理。

负责会话线程（Thread）的创建与查询，以及最近消息历史的获取。
Phase 0.5A: 合并新 (turn_id NOT NULL) 和旧 (turn_id IS NULL) Turn 的事件。
Phase 1: keyset-pagination bounded history reading.
"""
import json as _json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from aiive.db.models import TurnRecord, Event, Thread
from aiive.runtime.message_normalizer import normalize_tool_call, normalize_tool_result


@dataclass
class BoundedReadStats:
    pages_read: int = 0
    turns_included: int = 0
    total_events: int = 0
    total_bytes: int = 0
    estimated_tokens: int = 0
    stopped_by: str | None = None


class ThreadState:
    """线程状态管理器。"""

    def __init__(self, db: Session):
        self._db: Session = db

    def get_or_create_thread(self, thread_id: str | None = None) -> Thread:
        if thread_id:
            thread = self._db.get(Thread, thread_id)
            if thread:
                return thread
        thread = Thread(id=str(uuid.uuid4()))
        self._db.add(thread)
        self._db.flush()
        return thread

    # ── Phase 1: bounded keyset-pagination history ──

    def load_recent_messages_bounded(
        self,
        db: Session,
        thread_id: str,
        token_budget: int,
        token_counter: Any,  # TokenCounter Protocol
        model: str,
        normalizer: Any | None = None,
        upper_bound_sequence: int | None = None,
        max_pages: int = 20,
        max_turns: int = 100,
        max_events: int = 5000,
        max_raw_bytes: int = 2 * 1024 * 1024,
    ) -> tuple[list[dict[str, Any]], BoundedReadStats]:
        """Keyset DESC pagination, online estimation, stop at any limit."""
        all_turns: list[list[dict[str, Any]]] = []
        current_tokens = 0
        pages_read = 0
        total_events = 0
        total_bytes = 0
        stopped_by: str | None = None

        last_sequence: int | None = upper_bound_sequence
        page_size = 20

        while pages_read < max_pages and len(all_turns) < max_turns:
            base_query = (
                db.query(TurnRecord)
                .filter(
                    TurnRecord.thread_id == thread_id,
                    TurnRecord.status == "completed",
                    TurnRecord.turn_id.notilike("system_%"),
                    TurnRecord.turn_id.notilike("runtime_%"),
                )
            )
            if last_sequence is not None:
                base_query = base_query.filter(TurnRecord.turn_sequence < last_sequence)

            turns_page = (
                base_query
                .order_by(TurnRecord.turn_sequence.desc())
                .limit(page_size)
                .all()
            )
            pages_read += 1
            if not turns_page:
                break

            for turn in turns_page:
                last_sequence = turn.turn_sequence
                if len(all_turns) >= max_turns:
                    stopped_by = "max_turns"
                    break

                events = (
                    db.query(Event)
                    .filter(
                        Event.thread_id == thread_id,
                        Event.turn_id == turn.turn_id,
                        Event.event_type.in_([
                            "user_message", "tool_call", "tool_result",
                            "llm_response",
                        ]),
                    )
                    .order_by(Event.turn_event_index.asc())
                    .all()
                )
                total_events += len(events)
                if total_events > max_events:
                    stopped_by = "max_events"
                    break

                turn_dicts = self._events_to_dicts(events)
                turn_bytes = sum(len(_json.dumps(d, ensure_ascii=False, default=str).encode()) for d in turn_dicts)
                total_bytes += turn_bytes
                if total_bytes > max_raw_bytes:
                    stopped_by = "max_raw_bytes"
                    break

                # Normalize tool results before counting
                if normalizer is not None:
                    turn_dicts = normalizer.normalize_turn(turn_dicts)

                turn_msgs = self._dicts_to_chat_messages(turn_dicts)
                tc = token_counter.count_messages(model, turn_msgs)
                if current_tokens + tc.safe_tokens > token_budget:
                    if normalizer is not None:
                        turn_dicts_sparse = normalizer.normalize_turn_sparse(turn_dicts)
                        if turn_dicts_sparse != turn_dicts:
                            turn_msgs = self._dicts_to_chat_messages(turn_dicts_sparse)
                            tc = token_counter.count_messages(model, turn_msgs)
                            if current_tokens + tc.safe_tokens > token_budget:
                                stopped_by = "token_budget"
                                break
                            turn_dicts = turn_dicts_sparse
                        else:
                            stopped_by = "token_budget"
                            break
                    else:
                        stopped_by = "token_budget"
                        break

                current_tokens += tc.safe_tokens
                all_turns.append(turn_dicts)

            if stopped_by:
                break

        all_turns.reverse()
        result: list[dict[str, Any]] = []
        for tds in all_turns:
            result.extend(tds)

        return result, BoundedReadStats(
            pages_read=pages_read,
            turns_included=len(all_turns),
            total_events=total_events,
            total_bytes=total_bytes,
            estimated_tokens=current_tokens,
            stopped_by=stopped_by,
        )

    @staticmethod
    def _dicts_to_chat_messages(turn_dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Quick conversion for token counting only."""
        msgs: list[dict[str, Any]] = []
        for d in turn_dicts:
            etype = d.get("type", "")
            content = d.get("content", "")
            if etype == "user" and content:
                msgs.append({"role": "user", "content": content})
            elif etype == "assistant" and content:
                msgs.append({"role": "assistant", "content": content})
            elif etype == "tool_call":
                call = normalize_tool_call(d)
                msgs.append({"role": "assistant", "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": _json.dumps(call["args"], ensure_ascii=False),
                    },
                }]})
            elif etype in ("tool_result", "tool_result_ref"):
                normalized = normalize_tool_result(d)
                tr = normalized["result"]
                tr_str = tr if isinstance(tr, str) else _json.dumps(tr, ensure_ascii=False, default=str)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": normalized["id"],
                    "name": normalized["name"],
                    "content": tr_str,
                })
        return msgs

    # ── 前端历史分页 ──

    def list_thread_messages_page(
        self,
        thread_id: str,
        page_size: int = 50,
        before_sequence: int | None = None,
    ) -> dict[str, Any]:
        """按 Turn keyset 分页返回前端展示消息，不参与 LLM 上下文选择。"""
        size = max(1, min(page_size, 100))
        query = self._db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.status.in_([
                "not_started", "running", "completed", "interrupted_unknown",
                "failed", "cancelled", "preempted",
            ]),
            TurnRecord.turn_id.notilike("system_%"),
            TurnRecord.turn_id.notilike("runtime_%"),
        )
        if before_sequence is not None:
            query = query.filter(TurnRecord.turn_sequence < before_sequence)
        turns_desc = query.order_by(TurnRecord.turn_sequence.desc()).limit(size + 1).all()
        has_more = len(turns_desc) > size
        selected_desc = turns_desc[:size]

        messages: list[dict[str, Any]] = []
        for turn in reversed(selected_desc):
            events = (
                self._db.query(Event)
                .filter(
                    Event.thread_id == thread_id,
                    Event.turn_id == turn.turn_id,
                    Event.event_type.in_([
                        "user_message", "tool_call", "tool_result", "llm_response",
                        "maintenance_report_terminal",
                    ]),
                )
                .order_by(Event.turn_event_index.asc(), Event.created_at.asc(), Event.id.asc())
                .all()
            )
            messages.extend(self._events_to_display_messages(events, turn.status))

        # 到达新 Turn 历史末端时，继续拼接旧版 turn_id=NULL 事件。
        if not has_more:
            legacy_groups = self._query_legacy_groups(thread_id, size)
            legacy_items = [item for _, group in legacy_groups for item in group]
            legacy_messages = [
                {
                    "role": item["type"],
                    "content": item.get("content", ""),
                    "event_id": item.get("event_id", ""),
                    "trace_id": item.get("trace_id", ""),
                    "action_cards": item.get("action_cards", []),
                    "tool_calls": [],
                }
                for item in legacy_items
                if item.get("type") in ("user", "assistant") and item.get("content")
            ]
            messages = legacy_messages + messages

        next_cursor = selected_desc[-1].turn_sequence if has_more and selected_desc else None
        return {"messages": messages, "next_cursor": next_cursor, "has_more": has_more}

    @staticmethod
    def _events_to_display_messages(
        events: list[Event],
        turn_status: str = "completed",
    ) -> list[dict[str, Any]]:
        """把单个 Turn 的真实事件聚合为前端消息，并规范化工具状态。"""
        messages: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        call_by_id: dict[str, dict[str, Any]] = {}
        maintenance_cards: dict[str, dict[str, Any]] = {}
        last_tool_event: Event | None = None
        tools_attached = False
        for event in events:
            payload = event.payload or {}
            if event.event_type == "user_message" and payload.get("content"):
                messages.append({
                    "role": "user", "content": payload["content"],
                    "event_id": event.id, "trace_id": event.trace_id,
                    "action_cards": [], "pending_operations": [], "tool_calls": [],
                })
            elif event.event_type == "tool_call":
                last_tool_event = event
                call = {
                    "tool_call_id": payload.get("tool_call_id", ""),
                    "name": payload.get("name", ""),
                    "params": payload.get("params", {}),
                    "status": "pending",
                    "result": None,
                }
                calls.append(call)
                if call["tool_call_id"]:
                    call_by_id[str(call["tool_call_id"])] = call
            elif event.event_type == "tool_result":
                last_tool_event = event
                call_id = str(payload.get("tool_call_id", "") or "")
                call = call_by_id.get(call_id)
                if call is None:
                    call = next((
                        item for item in reversed(calls)
                        if item["name"] == payload.get("name") and item["status"] == "pending"
                    ), None)
                if call is None:
                    call = {
                        "tool_call_id": call_id,
                        "name": payload.get("name", ""),
                        "params": payload.get("params", {}),
                        "status": "execution_unknown",
                        "result": None,
                    }
                    calls.append(call)
                    if call_id:
                        call_by_id[call_id] = call
                call["status"] = ThreadState._normalize_tool_status(payload.get("status"))
                call["result"] = payload.get("result")
            elif event.event_type == "maintenance_report_terminal":
                operation_id = str(payload.get("operation_id", "") or "")
                card = payload.get("card")
                if operation_id and isinstance(card, dict):
                    maintenance_cards[operation_id] = card
            elif event.event_type == "llm_response" and payload.get("content"):
                messages.append({
                    "role": "assistant", "content": payload["content"],
                    "event_id": event.id, "trace_id": event.trace_id,
                    "action_cards": payload.get("action_cards", []),
                    "pending_operations": payload.get("pending_operations", []),
                    "tool_calls": calls,
                })
                tools_attached = True

        if maintenance_cards:
            for message in messages:
                cards = list(message.get("action_cards", []))
                for index, card in enumerate(cards):
                    if not isinstance(card, dict) or card.get("card_type") != "maintenance_report":
                        continue
                    operation_id = str((card.get("resource_refs") or {}).get("operation_id", "") or "")
                    if operation_id in maintenance_cards:
                        cards[index] = maintenance_cards[operation_id]
                message["action_cards"] = cards

        if turn_status != "running":
            for call in calls:
                if call["status"] == "pending":
                    call["status"] = "execution_unknown"

        if calls and not tools_attached and last_tool_event is not None:
            messages.append({
                "role": "assistant", "content": "",
                "event_id": last_tool_event.id, "trace_id": last_tool_event.trace_id,
                "action_cards": [], "pending_operations": [], "tool_calls": calls,
            })
        return messages

    @staticmethod
    def _normalize_tool_status(status: Any) -> str:
        """规范化历史工具状态；无法确认时不得声明完成。"""
        value = str(status or "unknown").lower()
        if value in {"completed", "committed", "succeeded"}:
            return "completed"
        if value in {"failed", "error", "execution_failed"}:
            return "failed"
        if value in {"pending", "pending_approval"}:
            return "pending"
        if value in {"cancelled", "preempted"}:
            return "cancelled"
        return "execution_unknown"

    def _query_legacy_groups(
        self, thread_id: str, overscan: int,
    ) -> list[tuple[tuple[datetime, int], list[dict[str, Any]]]]:
        """查询 turn_id=NULL 的旧事件并按 user_message 边界分组。

        sort_key = (earliest_created_at, MAX_BIGINT)  用于与新 Turn 合并排序。
        """
        events = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.turn_id.is_(None),
                Event.event_type.in_([
                    "user_message", "tool_call", "tool_result", "llm_response",
                ]),
            )
            .order_by(Event.created_at.asc(), Event.id.asc())
            .limit(overscan * 10)
            .all()
        )
        if not events:
            return []

        groups: list[list[Event]] = []
        current: list[Event] = []
        for e in events:
            if e.event_type == "user_message":
                if current:
                    groups.append(current)
                current = [e]
            else:
                current.append(e)
        if current:
            groups.append(current)

        MAX_INT = 2 ** 63 - 1
        result: list[tuple[tuple[datetime, int], list[dict[str, Any]]]] = []
        for g in groups[-overscan:]:
            earliest = g[0].created_at
            result.append(((earliest, MAX_INT), self._events_to_dicts(g)))
        return result

    @staticmethod
    def _events_to_dicts(events: list[Event]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for event in events:
            p = event.payload or {}
            etype = event.event_type
            if etype == "user_message":
                content = p.get("content", "")
                if content:
                    messages.append({"type": "user", "content": content, "event_id": event.id, "trace_id": event.trace_id})
            elif etype == "tool_call":
                messages.append({"type": "tool_call", "tool_name": p.get("name", ""), "tool_params": p.get("params", {}), "event_id": event.id, "trace_id": event.trace_id, "tool_call_id": p.get("tool_call_id", ""), "batch_index": p.get("batch_index", 0)})
            elif etype == "tool_result":
                messages.append({"type": "tool_result", "tool_name": p.get("name", ""), "tool_result": p.get("result", {}), "tool_status": p.get("status", "unknown"), "event_id": event.id, "trace_id": event.trace_id, "tool_call_id": p.get("tool_call_id", ""), "batch_index": p.get("batch_index", 0)})
            elif etype == "llm_response":
                content = p.get("content", "")
                if content:
                    messages.append({"type": "assistant", "content": content, "action_cards": p.get("action_cards", []), "event_id": event.id, "trace_id": event.trace_id})
        return messages
