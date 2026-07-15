"""Phase 0.5B: HandlerRegistry with schema_version validation."""

from __future__ import annotations

from collections.abc import Callable

from aiive.worker.outbox_dto import ClaimedJob, HandlerResult


class HandlerRegistry:
    """Handler 注册表，含每个 job_type 的 supported_schema_versions。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[ClaimedJob], HandlerResult]] = {}
        self._supported_versions: dict[str, frozenset[int]] = {}

    def register(
        self,
        job_type: str,
        handler: Callable[[ClaimedJob], HandlerResult],
        supported_schema_versions: frozenset[int],
    ) -> None:
        self._handlers[job_type] = handler
        self._supported_versions[job_type] = supported_schema_versions

    def get(self, job_type: str) -> Callable[[ClaimedJob], HandlerResult] | None:
        return self._handlers.get(job_type)

    def is_schema_supported(self, job_type: str, schema_version: int) -> bool:
        versions = self._supported_versions.get(job_type)
        return versions is not None and schema_version in versions
