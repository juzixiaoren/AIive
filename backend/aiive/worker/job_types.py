"""Outbox job type 目录；不再让 Worker 依赖 memory 子系统配置。"""
from aiive.memory.recall_config import ENABLED_OUTBOX_JOB_TYPES as _LEGACY_TYPES

AGENT_TASK_JOB_TYPES: frozenset[str] = frozenset({"agent_task_run"})
# 知识库维护：孤儿分块/失效文档/对象引用的幂等清理（复用 Outbox，不另建任务框架）
KNOWLEDGE_JOB_TYPES: frozenset[str] = frozenset({"knowledge_maintenance"})
ENABLED_OUTBOX_JOB_TYPES: frozenset[str] = _LEGACY_TYPES | AGENT_TASK_JOB_TYPES | KNOWLEDGE_JOB_TYPES
