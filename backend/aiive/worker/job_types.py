"""Outbox job type 目录；不再让 Worker 依赖 memory 子系统配置。"""
from aiive.memory.recall_config import ENABLED_OUTBOX_JOB_TYPES as _LEGACY_TYPES

AGENT_TASK_JOB_TYPES: frozenset[str] = frozenset({"agent_task_run"})
ENABLED_OUTBOX_JOB_TYPES: frozenset[str] = _LEGACY_TYPES | AGENT_TASK_JOB_TYPES
