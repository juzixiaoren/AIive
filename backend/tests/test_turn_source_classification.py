"""回归测试：消息来源统一由 TurnRecord.source 结构化判定。

覆盖两类路径：
- 前端展示（list_thread_messages_page / _events_to_display_messages）：
  system 类来源隐藏输入、展示 agent 回复。
- LLM 上下文（load_recent_messages_bounded / _events_to_dicts +
  ContextAssembler._dicts_to_chat_messages）：system 类输入以 system 角色注入。

同时验证 turn_id 前缀约定已废除（不再参与过滤）。
"""
from __future__ import annotations

import types

from sqlalchemy.orm import Session

from aiive.db.models import Event
from aiive.runtime.context_assembler import ContextAssembler
from aiive.runtime.thread_state import ThreadState
from tests._util import add_event, add_turn, new_thread


class _FakeTokenCounter:
    """返回极小 token 数，确保一次通过预算。"""

    def count_messages(self, model, messages, tools=None):
        return types.SimpleNamespace(safe_tokens=1, estimated_tokens=1)


def _user_message_event(db: Session, thread_id: str, turn_id: str, content: str, source: str) -> Event:
    return add_event(
        db, thread_id, turn_id, "user_message",
        {"content": content, "message_source": source},
    )


def _reply_event(db: Session, thread_id: str, turn_id: str, content: str) -> Event:
    return add_event(
        db, thread_id, turn_id, "llm_response", {"content": content},
    )


def test_display_hides_system_input_shows_reply(db) -> None:
    """system_command 来源的触发输入不在前端展示，但 agent 回复展示。"""
    thread = new_thread(db)
    turn = add_turn(db, thread.id, None, 1, status="completed",
                    turn_id="t-sys", source="system_command")
    _user_message_event(db, thread.id, turn.turn_id, "执行系统指令", "system_command")
    _reply_event(db, thread.id, turn.turn_id, "已执行")

    page = ThreadState(db).list_thread_messages_page(thread.id, page_size=10)
    contents = [m["content"] for m in page["messages"]]
    assert "执行系统指令" not in contents
    assert "已执行" in contents


def test_display_shows_user_input(db) -> None:
    """user 来源的输入与回复均在前端展示。"""
    thread = new_thread(db)
    turn = add_turn(db, thread.id, None, 1, status="completed",
                    turn_id="t-user", source="user")
    _user_message_event(db, thread.id, turn.turn_id, "你好", "user")
    _reply_event(db, thread.id, turn.turn_id, "你好呀")

    page = ThreadState(db).list_thread_messages_page(thread.id, page_size=10)
    contents = [m["content"] for m in page["messages"]]
    assert "你好" in contents
    assert "你好呀" in contents


def test_context_injects_system_role_for_system_source(db) -> None:
    """system_command 来源的输入以 system 角色进入 LLM 上下文。"""
    thread = new_thread(db)
    user_turn = add_turn(db, thread.id, None, 1, status="completed",
                         turn_id="t-user", source="user")
    _user_message_event(db, thread.id, user_turn.turn_id, "普通提问", "user")
    _reply_event(db, thread.id, user_turn.turn_id, "普通回答")

    sys_turn = add_turn(db, thread.id, None, 2, status="completed",
                        turn_id="t-sys", source="system_command")
    _user_message_event(db, thread.id, sys_turn.turn_id, "系统指令", "system_command")
    _reply_event(db, thread.id, sys_turn.turn_id, "系统回复")

    result, _ = ThreadState(db).load_recent_messages_bounded(
        db, thread.id, token_budget=10_000_000,
        token_counter=_FakeTokenCounter(), model="deepseek-chat",
    )
    msgs = ContextAssembler._dicts_to_chat_messages(result)

    roles = [(m["role"], m["content"]) for m in msgs]
    assert ("system", "系统指令") in roles
    assert ("assistant", "系统回复") in roles
    assert ("user", "普通提问") in roles


def test_runtime_event_source_classified_as_system(db) -> None:
    """runtime_event 来源同样按 system 处理：上下文标 system、展示隐藏输入。"""
    thread = new_thread(db)
    turn = add_turn(db, thread.id, None, 1, status="completed",
                    turn_id="t-rt", source="runtime_event")
    _user_message_event(db, thread.id, turn.turn_id, "[Reminder triggered]", "runtime_event")
    _reply_event(db, thread.id, turn.turn_id, "提醒你啦")

    result, _ = ThreadState(db).load_recent_messages_bounded(
        db, thread.id, token_budget=10_000_000,
        token_counter=_FakeTokenCounter(), model="deepseek-chat",
    )
    msgs = ContextAssembler._dicts_to_chat_messages(result)
    assert ("system", "[Reminder triggered]") in [(m["role"], m["content"]) for m in msgs]

    page = ThreadState(db).list_thread_messages_page(thread.id, page_size=10)
    contents = [m["content"] for m in page["messages"]]
    assert "[Reminder triggered]" not in contents
    assert "提醒你啦" in contents
