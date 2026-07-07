from enum import StrEnum


class MemoryType(StrEnum):
    USER_PROFILE = "user_profile"
    PREFERENCE = "preference"
    ROUTINE = "routine"
    FACT = "fact"
    PROJECT = "project"
    AGENT_SELF = "agent_self"
    POLICY = "policy"
    ENVIRONMENT = "environment"
    NAME = "name"
    HABIT = "habit"
    SCHEDULE = "schedule"


ALL_VALID_TYPES = frozenset(t.value for t in MemoryType)


def is_valid_memory_type(value: str) -> bool:
    return value in ALL_VALID_TYPES


PERSONAL_SIGNAL_TYPES = frozenset({
    MemoryType.USER_PROFILE.value,
    MemoryType.PREFERENCE.value,
    MemoryType.ROUTINE.value,
    MemoryType.HABIT.value,
    MemoryType.SCHEDULE.value,
})
