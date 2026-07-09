"""测试记忆类型（MemoryType）枚举及其验证函数。

覆盖标准类型定义、有效性检查和信号类型过滤。
"""

from aiive.memory.memory_types import (
    ALL_VALID_TYPES,
    PERSONAL_SIGNAL_TYPES,
    MemoryType,
    is_valid_memory_type,
)


class TestMemoryTypes:
    """测试 MemoryType 枚举的完整性和辅助函数。"""

    def test_all_standard_types_present(self):
        """验证所有标准记忆类型均已定义。"""
        expected = {
            "user_profile", "preference", "routine", "fact",
            "project", "agent_self", "policy", "environment",
            "name", "habit", "schedule",
        }
        assert set(MemoryType) == expected

    def test_is_valid_memory_type(self):
        """is_valid_memory_type 应正确识别有效和无效类型。"""
        assert is_valid_memory_type("fact")
        assert is_valid_memory_type("routine")
        assert is_valid_memory_type("preference")
        assert not is_valid_memory_type("invalid_type")

    def test_personal_signal_types(self):
        """个人信号类型应包含 routine、preference、user_profile、habit、schedule，不包含 fact。"""
        assert "routine" in PERSONAL_SIGNAL_TYPES
        assert "preference" in PERSONAL_SIGNAL_TYPES
        assert "user_profile" in PERSONAL_SIGNAL_TYPES
        assert "habit" in PERSONAL_SIGNAL_TYPES
        assert "schedule" in PERSONAL_SIGNAL_TYPES
        assert "fact" not in PERSONAL_SIGNAL_TYPES
