from aiive.memory.memory_types import (
    ALL_VALID_TYPES,
    PERSONAL_SIGNAL_TYPES,
    MemoryType,
    is_valid_memory_type,
)


class TestMemoryTypes:
    def test_all_standard_types_present(self):
        expected = {
            "user_profile", "preference", "routine", "fact",
            "project", "agent_self", "policy", "environment",
            "name", "habit", "schedule",
        }
        assert set(MemoryType) == expected

    def test_is_valid_memory_type(self):
        assert is_valid_memory_type("fact")
        assert is_valid_memory_type("routine")
        assert is_valid_memory_type("preference")
        assert not is_valid_memory_type("invalid_type")

    def test_personal_signal_types(self):
        assert "routine" in PERSONAL_SIGNAL_TYPES
        assert "preference" in PERSONAL_SIGNAL_TYPES
        assert "user_profile" in PERSONAL_SIGNAL_TYPES
        assert "habit" in PERSONAL_SIGNAL_TYPES
        assert "schedule" in PERSONAL_SIGNAL_TYPES
        assert "fact" not in PERSONAL_SIGNAL_TYPES
