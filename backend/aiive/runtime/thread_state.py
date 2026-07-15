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
                msgs.append({"role": "assistant", "tool_calls": [{
                    "type": "function",
                    "function": {
                        "name": d.get("tool_name", ""),
                        "arguments": _json.dumps(d.get("tool_params", {})),
                    },
                }]})
            elif etype in ("tool_result", "tool_result_ref"):
                tr = d.get("tool_result", {})
                tr_str = tr if isinstance(tr, str) else _json.dumps(tr, ensure_ascii=False, default=str)
                msgs.append({"role": "tool", "content": tr_str})
        return msgs

    # ── Legacy methods ──

    def get_recent_messages(
        self, thread_id: str, max_turns: int = 20,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """合并新 Turn 和旧 Turn 事件。

        limit: backward-compat, overrides max_turns when smaller.
        """
        effective_max = max_turns
        if limit is not None and limit < effective_max:
            effective_max = limit
        """合并新 Turn (turn_id NOT NULL) 和旧 Turn (turn_id IS NULL) 的事件。

        排序: 每组计算 sort_key = (earliest_created_at, turn_sequence 或 MAX_BIGINT)
        按 sort_key 取最后 max_turns 组（最近），保持时间正序。
        """
        # ── 新 Turn: 按 turn_sequence DESC 取最近 completed Turn ──
        new_turns = (
            self._db.query(TurnRecord)
            .filter(
                TurnRecord.thread_id == thread_id,
                TurnRecord.status == "completed",
            )
            .order_by(TurnRecord.turn_sequence.desc())
            .limit(effective_max * 2)
            .all()
        )
        new_turns_asc = list(reversed(new_turns))

        new_groups: list[tuple[tuple[Any, int], list[dict[str, Any]]]] = []
        for turn in new_turns_asc:
            events = (
                self._db.query(Event)
                .filter(
                    Event.thread_id == thread_id,
                    Event.turn_id == turn.turn_id,
                    Event.event_type.in_([
                        "user_message", "tool_call", "tool_result", "llm_response",
                    ]),
                )
                .order_by(Event.turn_event_index.asc())
                .all()
            )
            if events:
                earliest = events[0].created_at
                sort_key = (earliest, turn.turn_sequence)
                new_groups.append((sort_key, self._events_to_dicts(events)))

        # ── 旧 Turn: by user_message boundary ──
        legacy_groups = self._query_legacy_groups(thread_id, effective_max * 3)

        # ── 合并排序 ──
        all_groups = legacy_groups + new_groups
        all_groups.sort(key=lambda g: g[0])

        # ── 取最后 effective_max 个（最近）──
        selected = all_groups[-effective_max:] if len(all_groups) > effective_max else all_groups

        result: list[dict[str, Any]] = []
        for _, events in selected:
            result.extend(events)
        return result

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
