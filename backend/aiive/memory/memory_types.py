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


# 记忆键规范：写记忆时的唯一事实来源（显式工具与自动提取器都引用它）。
# 不属于 Runtime Identity，也不常驻系统提示词。
MEMORY_KEY_GUIDE = """记忆键规范（写记忆时使用，同 key 自动覆盖旧记忆）：
- agent.display_name: Agent 可见名称。触发："以后你叫X" → content="X"（纯值）
- user.display_name: 用户称呼。触发："以后叫我X" → content="X"（纯值）
- user.name: 用户真实姓名，仅当用户明确说"我的真实姓名是X"。content="X"（纯值）
- user.preference.response_style: 回复风格偏好。触发："我喜欢你回答简洁一点"
- agent.persona.tone: Agent 语气/人格。触发："你以后说话活泼一点"
- agent.persona.relationship: 主从/关系风格（"你是我的主人""我是你的上司"）。严禁写进 display_name 或 user.name
- user.preference.<topic>: 用户某主题偏好
- project.<project_name>.<topic>: 项目决策。触发："AIive 后端用 FastAPI" → project.aiive.backend_stack
身份键（user.name/user.display_name/agent.display_name/agent.persona.*）的 content 必须是纯值，不要含"我的名字叫""以后叫我"等前缀，也不要把关系表述（"你是我的主人"）塞进这些键。"""
