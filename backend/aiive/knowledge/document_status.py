"""知识文档生命周期状态；摄取、向量索引、维护与 API 共用的领域定义。"""

# 活跃状态：分块参与检索。
ACTIVE_STATUSES: frozenset[str] = frozenset({"indexed", "index_failed", "source_unavailable"})
# 可自动修复的活跃状态：原文对象应当仍可读取，允许重建分块
RECOVERABLE_STATUSES: frozenset[str] = frozenset({"indexed", "index_failed"})
# 失效状态：分块必须为空，且绝不参与检索。
INACTIVE_STATUSES: frozenset[str] = frozenset({"superseded", "deleted"})
DOCUMENT_STATUSES: frozenset[str] = ACTIVE_STATUSES | INACTIVE_STATUSES
