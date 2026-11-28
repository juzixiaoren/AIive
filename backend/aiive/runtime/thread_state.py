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

from aiive.core.assistant_protocol import PROVIDER_METADATA
from aiive.db.models import Event, LLMCall, Thread, TurnRecord
from aiive.forget.visibility_service import ForgetVisibilityService
from aiive.runtime.message_normalizer import (
    compact_web_search_result,
    event_stamp,
    normalize_tool_call,
    normalize_tool_result,
    tool_result_content,
)


@dataclass
class BoundedReadStats:
    pages_read: int = 0
    turns_included: int = 0
    total_events: int = 0
    total_bytes: int = 0
    estimated_tokens: int = 0
    stopped_by: str | None = None


def _history_bytes(items: list[dict[str, Any]]) -> int:
    return sum(len(_json.dumps(item, ensure_ascii=False, default=str).encode()) for item in items)


class ThreadState:
    """线程状态管理器。"""

    def __init__(self, db: Session):
        self._db: Session = db

    def get_or_create_thread(self, thread_id: str | None = None) -> Thread:
        if thread_id:
            thread = self._db.get(Thread, thread_id)
            if thread:
                return thread
        # 指定了 thread_id 但不存在时，用请求的 id 创建（而非静默换一个新 id，
        # 否则调用方持有的 thread_id 与实际落库线程分裂）。
        thread = Thread(id=thread_id or str(uuid.uuid4()))
        self._db.add(thread)
        self._db.flush()
        return thread

    # ── Phase 1: bounded keyset-pagination history ──

    def load_recent_messages_bounded(
        self,
        db: Session,
        thread_id: str,
        token_budget: int | None,
        token_counter: Any,  # TokenCounter Protocol
        model: str,
        normalizer: Any | None = None,
        upper_bound_sequence: int | None = None,
        lower_bound_sequence: int | None = None,
        segment_id: str | None = None,
        max_pages: int = 20,
        max_turns: int = 100,
        max_events: int = 5000,
        max_raw_bytes: int = 2 * 1024 * 1024,
        *,
        compact_web_search_results: bool = False,
        zone: str = "UTC",
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

        def include_turn(turn_dicts: list[dict[str, Any]], *, interrupted: bool = False) -> bool:
            nonlocal current_tokens
            if normalizer is not None:
                turn_dicts = normalizer.normalize_turn(turn_dicts)
            if interrupted:
                turn_dicts = self._degrade_interrupted_turn_dicts(turn_dicts, zone)
                if not turn_dicts:
                    return True
            turn_msgs = self._dicts_to_token_count_messages(turn_dicts)
            tc = token_counter.count_messages(model, turn_msgs)
            if token_budget is not None and current_tokens + tc.safe_tokens > token_budget:
                if normalizer is not None:
                    turn_dicts = normalizer.normalize_turn_sparse(turn_dicts)
                    turn_msgs = self._dicts_to_token_count_messages(turn_dicts)
                    tc = token_counter.count_messages(model, turn_msgs)
                if current_tokens + tc.safe_tokens > token_budget:
                    return False
            current_tokens += tc.safe_tokens
            all_turns.append(turn_dicts)
            return True

        while pages_read < max_pages and len(all_turns) < max_turns:
            # interrupted_unknown Turn 也纳入历史：其中断前已持久化的工具事实
            # （副作用可能已发生）以降级 system 文本注入，避免模型遗忘后重复执行。
            base_query = (
                db.query(TurnRecord)
                .filter(
                    TurnRecord.thread_id == thread_id,
                    TurnRecord.status.in_(("completed", "interrupted_unknown")),
                )
            )
            if segment_id is not None:
                base_query = base_query.filter(TurnRecord.segment_id == segment_id)
            if last_sequence is not None:
                base_query = base_query.filter(TurnRecord.turn_sequence < last_sequence)
            if lower_bound_sequence is not None:
                base_query = base_query.filter(
                    TurnRecord.turn_sequence > lower_bound_sequence,
                )

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
                    .order_by(Event.turn_event_index.asc().nulls_last(), Event.created_at.asc(), Event.id.asc())
                    .limit(max_events - total_events + 1)
                    .all()
                )
                total_events += len(events)
                if total_events > max_events:
                    stopped_by = "max_events"
                    break

                blocked = ForgetVisibilityService.blocked_target_ids(
                    db, "event", [(event.id, event.created_at) for event in events],
                )
                turn_dicts = self._events_to_dicts(
                    [event for event in events if event.id not in blocked], turn.source, turn.created_at,
                )
                total_bytes += _history_bytes(turn_dicts)
                if total_bytes > max_raw_bytes:
                    stopped_by = "max_raw_bytes"
                    break

                if compact_web_search_results:
                    self._compact_search_results(turn_dicts)
                if not include_turn(turn_dicts, interrupted=turn.status == "interrupted_unknown"):
                    stopped_by = "token_budget"
                    break

            if stopped_by:
                break

        # Legacy events precede numbered turns and have no segment or sequence.
        if (
            compact_web_search_results and not stopped_by and pages_read < max_pages
            and lower_bound_sequence is None and segment_id is None
            and len(all_turns) < max_turns and total_events < max_events
        ):
            legacy = self._query_legacy_groups(
                thread_id, max_turns - len(all_turns),
                compact_web_search_results=True, max_events=max_events - total_events,
            )
            pages_read += 1
            for group, raw_bytes in reversed(legacy):
                total_events += len(group)
                total_bytes += raw_bytes
                if total_bytes > max_raw_bytes:
                    stopped_by = "max_raw_bytes"
                    break
                if not include_turn(group):
                    stopped_by = "token_budget"
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
            estimated_tokens=(token_counter.count_messages(
                model, self._dicts_to_token_count_messages(result),
            ).estimated_tokens if result else 0),
            stopped_by=stopped_by,
        )

    @staticmethod
    def _compact_search_results(turn_dicts: list[dict[str, Any]]) -> None:
        for item in turn_dicts:
            if item.get("type") == "tool_result" and item.get("tool_name") == "web_search":
                item["tool_result"] = compact_web_search_result(item["tool_result"])
                item["web_search_compacted"] = True

    @staticmethod
    def _degrade_interrupted_turn_dicts(
        turn_dicts: list[dict[str, Any]],
        zone: str = "UTC",
    ) -> list[dict[str, Any]]:
        """保留有协议元数据的中断批次，旧批次用有界说明记录副作用。"""
        if any(item.get("type") == "tool_call" and item.get(PROVIDER_METADATA) for item in turn_dicts):
            # The paired tool facts can be replayed with their original Assistant
            # metadata. Keep the interruption notice, including for partial batches.
            return [*turn_dicts, {
                "type": "system",
                "content": "[上一轮 Turn 在工具调用后中断，已记录的工具副作用可能已生效，请勿盲目重复执行]",
                "turn_id": turn_dicts[0].get("turn_id"),
                "message_source": "runtime_event",
                "protected": True,
            }]
        tool_lines: list[str] = []
        results_by_id: dict[str, dict[str, Any]] = {
            str(d.get("tool_call_id") or ""): d
            for d in turn_dicts if d.get("type") in ("tool_result", "tool_result_ref")
        }
        for d in turn_dicts:
            if d.get("type") != "tool_call":
                continue
            name = str(d.get("tool_name") or "")
            rd = results_by_id.get(str(d.get("tool_call_id") or ""))
            status = str(rd.get("tool_status") or "unknown") if rd else "unknown"
            result_text = ""
            if rd is not None:
                normalized = normalize_tool_result(rd)
                result_text = tool_result_content(normalized["result"], name)
            stamps = event_stamp(d, "tool_call", zone)
            if rd is not None:
                stamps += " " + event_stamp(rd, "tool_result", zone)
            tool_lines.append(f"- {stamps} {name} -> {status}: {result_text}")
        if not tool_lines:
            return []
        request = next(
            (d for d in turn_dicts if d.get("type") in ("user", "system")),
            None,
        )
        parts = ["[上一轮 Turn 被中断，以下工具调用在中断前已实际执行，其副作用可能已生效，请勿盲目重复执行]"]
        if request and request.get("content"):
            stamp = event_stamp(request, request.get("message_source") or request["type"], zone)
            parts.append(f"当时的请求: {stamp} {request['content']}")
        parts.extend(tool_lines)
        return [{"type": "system", "content": "\n".join(parts),
                 "turn_id": turn_dicts[0].get("turn_id"), "message_source": "runtime_event", "protected": True}]

    @staticmethod
    def _dicts_to_token_count_messages(turn_dicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Quick conversion for token counting only."""
        msgs: list[dict[str, Any]] = []
        for d in turn_dicts:
            etype = d.get("type", "")
            from aiive.runtime.attachments import render_attachment_refs
            content = render_attachment_refs(d.get("content", ""), d.get("attachments", []))
            if etype == "user" and content:
                msgs.append({"role": "user", "content": content})
            elif etype == "system" and content:
                msgs.append({"role": "system", "content": content})
            elif etype == "assistant" and (content or d.get(PROVIDER_METADATA)):
                msgs.append({"role": "assistant", "content": content})
                if metadata := d.get(PROVIDER_METADATA):
                    msgs[-1][PROVIDER_METADATA] = metadata
            elif etype == "tool_call":
                call = normalize_tool_call(d)
                msg: dict[str, Any] = {"role": "assistant", "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": _json.dumps(call["args"], ensure_ascii=False),
                    },
                }]}
                if metadata := d.get(PROVIDER_METADATA):
                    msg[PROVIDER_METADATA] = metadata
                if content := d.get("assistant_content"):
                    msg["content"] = content
                msgs.append(msg)
            elif etype in ("tool_result", "tool_result_ref"):
                normalized = normalize_tool_result(d)
                msgs.append({
                    "role": "tool",
                    "tool_call_id": normalized["id"],
                    "name": normalized["name"],
                    "content": tool_result_content(normalized["result"], normalized["name"]),
                })
        return msgs

    # ── 前端历史分页 ──

    def list_thread_messages_page(
        self,
        thread_id: str,
        page_size: int = 50,
        before_sequence: int | None = None,
        from_sequence: int | None = None,
    ) -> dict[str, Any]:
        """按 Turn keyset 分页返回前端展示消息，不参与 LLM 上下文选择。"""
        size = max(1, min(page_size, 100))
        query = self._db.query(TurnRecord).filter(
            TurnRecord.thread_id == thread_id,
            TurnRecord.status.in_([
                "not_started", "running", "completed", "interrupted_unknown",
                "failed", "cancelled", "preempted",
            ]),
        )
        if before_sequence is not None:
            query = query.filter(TurnRecord.turn_sequence < before_sequence)
        if from_sequence is not None:
            query = query.filter(TurnRecord.turn_sequence >= from_sequence)
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
            from aiive.forget.visibility_service import ForgetVisibilityService
            blocked = ForgetVisibilityService.blocked_target_ids(
                self._db, "event", [(event.id, event.created_at) for event in events])
            messages.extend(self._events_to_display_messages(
                [event for event in events if event.id not in blocked], turn.status, turn.source))

        # 到达新 Turn 历史末端时，继续拼接旧版 turn_id=NULL 事件。
        if not has_more and from_sequence is None:
            legacy_groups = self._query_legacy_groups(thread_id, size)
            legacy_items = [item for group, _ in legacy_groups for item in group]
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
        turn_source: str | None = None,
    ) -> list[dict[str, Any]]:
        """把单个 Turn 的真实事件聚合为前端消息，并规范化工具状态。

        turn_source 为结构化消息来源（TurnRecord.source）；为 None 时
        回退到事件 payload 的 message_source（兼容旧数据）。
        system 类来源只展示 agent 回复，隐藏其触发输入。
        """
        messages: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        call_by_id: dict[str, dict[str, Any]] = {}
        maintenance_cards: dict[str, dict[str, Any]] = {}
        last_tool_event: Event | None = None
        tools_attached = False
        for event in events:
            payload = event.payload or {}
            if payload.get("protocol_only"):
                continue
            if event.event_type == "user_message" and payload.get("content"):
                message_source = turn_source or payload.get("message_source")
                if message_source in (None, "user"):
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
        *, compact_web_search_results: bool = False, max_events: int | None = None,
    ) -> list[tuple[list[dict[str, Any]], int]]:
        """按 user_message 边界分组，模型投影前记录原始字节数。"""
        query = (
            self._db.query(Event)
            .filter(
                Event.thread_id == thread_id,
                Event.turn_id.is_(None),
                Event.event_type.in_([
                    "user_message", "tool_call", "tool_result", "llm_response",
                ]),
            )
        )
        if compact_web_search_results:
            events = query.order_by(Event.created_at.desc(), Event.id.desc()).limit(
                min(overscan * 10, max_events) if max_events is not None else overscan * 10,
            ).all()
            events.reverse()
        else:
            events = query.order_by(Event.created_at.asc(), Event.id.asc()).limit(overscan * 10).all()
        if not events:
            return []
        blocked = ForgetVisibilityService.blocked_target_ids(
            self._db, "event", [(event.id, event.created_at) for event in events])
        events = [event for event in events if event.id not in blocked]

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

        result: list[tuple[list[dict[str, Any]], int]] = []
        for g in groups[-overscan:]:
            items = self._events_to_dicts(g)
            raw_bytes = 0
            if compact_web_search_results:
                raw_bytes = _history_bytes(items)
                self._compact_search_results(items)
            result.append((items, raw_bytes))
        return result

    @staticmethod
    def _events_to_dicts(
        events: list[Event],
        turn_source: str | None = None,
        turn_created_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        from sqlalchemy.orm import object_session
        from aiive.runtime.attachments import event_attachment_refs
        db = object_session(events[0]) if events else None
        refs = event_attachment_refs(db, [e.id for e in events if e.event_type == "user_message"]) if db is not None else {}
        messages: list[dict[str, Any]] = []
        legacy_traces = {
            e.trace_id for e in events if e.event_type in ("llm_response", "tool_call")
            and not (e.payload or {}).get(PROVIDER_METADATA)
            and not (e.payload or {}).get("proactive")
            and not (e.payload or {}).get("approval_id")
        }
        compatible_traces = set()
        if db is not None and legacy_traces:
            calls = db.query(LLMCall.trace_id, LLMCall.model).filter(
                LLMCall.thread_id == events[0].thread_id, LLMCall.trace_id.in_(legacy_traces),
            ).all()
            # Old Main Agent used the prefix "deepseek/" for every model in LLMCall.
            # Trust a documented model family or explicit provider prefix, not that default.
            compatible = {"openai", "openai_compatible", "anthropic", "gemini", "google", "qwen", "dashscope", "ollama", "zhipu"}
            models_by_trace: dict[str, set[str]] = {}
            for trace_id, model in calls:
                models_by_trace.setdefault(trace_id, set()).add(model.lower())
            compatible_traces = {
                trace_id for trace_id, models in models_by_trace.items()
                if all(
                    model.partition("/")[0] in compatible
                    or model.rsplit("/", 1)[-1].startswith(
                        ("gpt-", "chatgpt-", "o1", "o3", "o4", "claude-", "gemini-", "qwen", "glm-", "kimi-", "llama", "mistral-")
                    )
                    for model in models
                )
            }
        for event in events:
            p = event.payload or {}
            etype = event.event_type
            common: dict[str, Any] = {
                "event_id": event.id, "trace_id": event.trace_id, "turn_id": event.turn_id,
                "turn_event_index": event.turn_event_index, "created_at": event.created_at,
                "turn_created_at": turn_created_at,
                "message_source": p.get("message_source") or turn_source,
            }
            if etype == "user_message":
                content = p.get("content", "")
                if content or refs.get(event.id):
                    source = turn_source or p.get("message_source")
                    role = "user" if source in (None, "user") else "system"
                    common["message_source"] = source or "user"
                    messages.append({"type": role, "content": content, "attachments": refs.get(event.id, []), **common})
                continue
            metadata = p.get(PROVIDER_METADATA)
            if not metadata:
                if p.get("approval_id") or p.get("proactive"):
                    metadata = {"provider": "aiive"}
                elif event.trace_id in compatible_traces:
                    metadata = {"provider": "openai_compatible"}
            if etype in ("tool_call", "tool_result"):
                common.update({
                    "type": etype, "tool_name": p.get("name", ""), "tool_call_id": p.get("tool_call_id", ""),
                    "batch_index": p.get("batch_index", 0),
                })
                for key in ("approval_id", "protocol_status"):
                    if key in p:
                        common[key] = p[key]
                if etype == "tool_call":
                    common["tool_params"] = p.get("params", {})
                    if p.get("assistant_content"):
                        common["assistant_content"] = p["assistant_content"]
                    if metadata:
                        common[PROVIDER_METADATA] = metadata
                else:
                    common.update(tool_result=p.get("result", {}), tool_status=p.get("status", "unknown"))
                messages.append(common)
            elif etype == "llm_response" and (p.get("content") or p.get(PROVIDER_METADATA)):
                common["message_source"] = "runtime_event" if p.get("proactive") else "assistant"
                message = {"type": "assistant", "content": p.get("content", ""), "action_cards": p.get("action_cards", []), **common}
                if metadata:
                    message[PROVIDER_METADATA] = metadata
                messages.append(message)
        return messages
