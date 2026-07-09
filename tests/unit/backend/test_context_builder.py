"""测试 ContextBuilder 上下文构建功能。"""
from aiive.core.context_builder import (
    STABLE_PREFIX,
    ContextBuilder,
    ContextItem,
    _compute_stable_prefix_hash,
)


class TestContextItem:
    """测试 ContextItem 的 token 估算和基本字段。"""

    def test_token_estimate(self):
        """验证 token 估算值大于 0。"""
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
        """验证空内容的 token 估算至少为 1。"""
        item = ContextItem(
            item_id="test",
            kind="stable_prefix",
            source="system",
            trust_level="trusted",
            content_preview="",
        )
        assert item.token_estimate == 1  # 最低为 1


class TestContextBuilder:
    """测试上下文构建、消息拼接和哈希一致性。"""

    def test_build_with_empty_history(self):
        """验证空历史时构建的消息和上下文项结构正确。"""
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
        """验证有历史记录时消息和上下文项数量正确。"""
        builder = ContextBuilder()
        history = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
        ]
        messages, items, _ = builder.build(history=history, current_message="Q2")

        # stable_prefix + 2 条历史 + 1 条当前 = 4
        assert len(messages) == 4
        assert len(items) == 4

    def test_stable_prefix_hash_stable(self):
        """验证 stable_prefix 哈希值稳定不变。"""
        h1 = _compute_stable_prefix_hash()
        h2 = _compute_stable_prefix_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_meta_includes_stable_prefix_hash(self):
        """验证 meta 中包含 stable_prefix 哈希。"""
        builder = ContextBuilder()
        _, _, meta = builder.build(history=[], current_message="Hi")
        assert "stable_prefix_hash" in meta
        assert meta["stable_prefix_hash"] == _compute_stable_prefix_hash()

    def test_all_items_have_required_fields(self):
        """验证所有上下文项都包含必需字段。"""
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
