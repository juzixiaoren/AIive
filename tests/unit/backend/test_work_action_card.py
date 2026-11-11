from aiive.runtime.tool_executor import ToolCallRecord, build_action_cards


def test_delegate_task_creates_distinct_work_card():
    record = ToolCallRecord(name="delegate_task", params={}, result={"ok": True, "result": {"task_id":"task-1", "title":"分析", "status":"queued", "summary":"已委派"}}, status="completed", trace_id="trace")
    cards = build_action_cards([record])
    assert len(cards) == 1
    assert cards[0].card_type == "agent_task_created"
    assert cards[0].resource_refs["task_id"] == "task-1"
    assert cards[0].actions[0].action == "view_work"


def test_failed_delegate_has_no_work_card():
    record = ToolCallRecord(name="delegate_task", params={}, result={"ok": False, "result": {"task_id":"task-1"}}, status="failed", trace_id="trace")
    assert all(card.card_type != "agent_task_created" for card in build_action_cards([record]))
