from aiive.core.context_builder import (
    STABLE_PREFIX,
    ContextBuilder,
    ContextItem,
    _compute_stable_prefix_hash,
)


class TestContextItem:
    def test_token_estimate(self):
        item = ContextItem(
            item_id="test",
            kind="stable_prefix",
            source="system",
            trust_level="trusted",
            content_preview="Hello World! This is a test.",
        )
        assert item.token_estimate > 0
        assert item.preview_length > 0

    def test_empty_content(self):
        item = ContextItem(
            item_id="test",
            kind="stable_prefix",
            source="system",
            trust_level="trusted",
            content_preview="",
        )
        assert item.token_estimate == 1  # floor at 1


class TestContextBuilder:
    def test_build_with_empty_history(self):
        builder = ContextBuilder()
        messages, items, meta = builder.build(history=[], current_message="Hi")

        assert len(items) >= 2  # stable_prefix + current_message
        assert items[0].kind == "stable_prefix"
        assert items[0].trust_level == "trusted"
        assert items[-1].kind == "user_message"
        assert items[-1].content_preview == "Hi"

        assert messages[0]["role"] == "system"
        assert STABLE_PREFIX in messages[0]["content"]
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "Hi"

    def test_build_with_history(self):
        builder = ContextBuilder()
        history = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ]
        messages, items, _ = builder.build(history=history, current_message="Q2")

        # stable_prefix + 2 history + 1 current = 4
        assert len(messages) == 4
        assert len(items) == 4

    def test_stable_prefix_hash_stable(self):
        h1 = _compute_stable_prefix_hash()
        h2 = _compute_stable_prefix_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_meta_includes_stable_prefix_hash(self):
        builder = ContextBuilder()
        _, _, meta = builder.build(history=[], current_message="Hi")
        assert "stable_prefix_hash" in meta
        assert meta["stable_prefix_hash"] == _compute_stable_prefix_hash()

    def test_truncation_when_history_exceeds_limit(self):
        builder = ContextBuilder(working_limit=3)
        history = [{"role": "user", "content": f"msg{i}"} for i in range(10)]
        messages, _, meta = builder.build(history=history, current_message="Hi")

        assert meta["truncated"] is True
        assert meta["truncated_from"] == 10
        # stable_prefix + 3 history + 1 current = 5 (but meta says truncated_from = history length)
        # working messages = last 3 from history
        assert meta["working_limit"] == 3
        # messages: system + 3 history + 1 user = 5
        assert len(messages) == 5

    def test_no_truncation_within_limit(self):
        builder = ContextBuilder(working_limit=10)
        history = [{"role": "user", "content": f"msg{i}"} for i in range(5)]
        _, _, meta = builder.build(history=history, current_message="Hi")

        assert meta["truncated"] is False
        assert meta["truncated_from"] == 0

    def test_all_items_have_required_fields(self):
        builder = ContextBuilder()
        _, items, _ = builder.build(
            history=[{"role": "user", "content": "Hello"}],
            current_message="World",
        )
        for item in items:
            assert item.item_id
            assert item.kind in ("stable_prefix", "history_message", "user_message")
            assert item.source
            assert item.trust_level
            assert item.token_estimate > 0
