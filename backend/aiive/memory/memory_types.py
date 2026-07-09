"""V20 MemoryTypes：记忆系统的类型定义。

记忆系统是 AIive 的核心组件，负责 Agent 的长期记忆管理和检索。
本模块定义了记忆系统中使用的所有枚举类型和常量集合。

记忆类型涵盖用户画像、偏好、日常习惯、项目信息、策略规则等，
为记忆提取、存储、检索和上下文构建提供统一的类型约定。
"""

from enum import StrEnum


class MemoryType(StrEnum):
    """记忆类型枚举，定义系统中所有合法的记忆分类。

    每种类型对应不同的记忆来源和处理方式：
    - USER_PROFILE: 用户身份信息（姓名、年龄、职业等）
    - PREFERENCE: 用户偏好（交互风格、喜好等）
    - ROUTINE: 用户日常规律（每日/每周行为模式）
    - FACT: 通用事实信息
    - PROJECT: 项目相关信息（技术栈、架构决策等）
    - AGENT_SELF: Agent 自身身份和人格信息
    - POLICY: 策略和规则约束
    - ENVIRONMENT: 环境配置信息
    - NAME: 名称信息
    - HABIT: 习惯信息
    - SCHEDULE: 日程安排信息
    """
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


# 所有合法记忆类型的不可变集合（用于快速验证）
ALL_VALID_TYPES = frozenset(t.value for t in MemoryType)


def is_valid_memory_type(value: str) -> bool:
    """验证给定的字符串是否为合法的记忆类型。

    Args:
        value: 待验证的类型字符串。

    Returns:
        是否为合法记忆类型。
    """
    return value in ALL_VALID_TYPES


# 个人信号记忆类型集合（用户相关的身份、偏好、习惯、日常、日程等）
# 这些类型由 StewardSignalExtractor 和 MemoryExtractor 专门处理
PERSONAL_SIGNAL_TYPES = frozenset({
    MemoryType.USER_PROFILE.value,
    MemoryType.PREFERENCE.value,
    MemoryType.ROUTINE.value,
    MemoryType.HABIT.value,
    MemoryType.SCHEDULE.value,
})
