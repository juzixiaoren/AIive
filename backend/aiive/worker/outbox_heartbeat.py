"""Phase 0.5B: ActiveClaimRegistry + OutboxHeartbeat.

ActiveClaimRegistry: thread-safe registry keyed by (job_id, claim_token).
OutboxHeartbeat: daemon thread that renews leases for registered claims.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from aiive.db.base import SessionLocal
from aiive.db.models import OutboxJob
from aiive.worker.outbox_dto import ActiveClaim

logger = logging.getLogger(__name__)

LEASE_DURATION = timedelta(seconds=120)
HEARTBEAT_INTERVAL = 30  # seconds between renewals


# ============================================================================
# ActiveClaimRegistry
# ============================================================================


class ActiveClaimRegistry:
    """线程安全的活跃 Claim 注册表，键为 (job_id, claim_token)。"""

    def __init__(self) -> None:
        self._claims: dict[tuple[str, str], ActiveClaim] = {}
        self._lock: threading.Lock = threading.Lock()

    def add(self, claim: ActiveClaim) -> None:
        """注册新 claim。如果同一 job_id 已有旧 claim，标记旧 claim lost。"""
        with self._lock:
            old_keys = [k for k in self._claims if k[0] == claim.job_id]
            for k in old_keys:
                old = self._claims.pop(k, None)
                if old:
                    old.lost = True
            self._claims[(claim.job_id, claim.claim_token)] = claim

    def remove(self, job_id: str, claim_token: str) -> None:
        """移除指定 (job_id, claim_token)。"""
        with self._lock:
            self._claims.pop((job_id, claim_token), None)

    def get_snapshot(self) -> list[ActiveClaim]:
        """返回当前所有活跃 Claim 的快照。"""
        with self._lock:
            return list(self._claims.values())

    def mark_lost(self, job_id: str, claim_token: str) -> None:
        """标记特定 Claim 已被接管。"""
        with self._lock:
            claim = self._claims.get((job_id, claim_token))
            if claim:
                claim.lost = True
                logger.warning(
                    "Claim marked lost: job_id=%s token=%s", job_id, claim_token,
                )



# ============================================================================
# OutboxHeartbeat
# ============================================================================


class OutboxHeartbeat:
    """租约守护线程：定期续租 ActiveClaimRegistry 中所有活跃 claim 的 OutboxJob。"""

    def __init__(
        self,
        worker_id: str,
        registry: ActiveClaimRegistry,
        interval: int = HEARTBEAT_INTERVAL,
    ) -> None:
        self._worker_id: str = worker_id
        self._registry: ActiveClaimRegistry = registry
        self._interval: int = interval
        self._stop_event: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="outbox-heartbeat",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            db: Session | None = None
            try:
                db = SessionLocal()
                now = datetime.now(timezone.utc)
                claims = self._registry.get_snapshot()
                for claim in claims:
                    affected = db.query(OutboxJob).filter(
                        OutboxJob.id == claim.job_id,
                        OutboxJob.claim_token == claim.claim_token,
                        OutboxJob.locked_by == self._worker_id,
                        OutboxJob.status == "running",
                    ).update({
                        OutboxJob.lease_expires_at: now + LEASE_DURATION,
                    }, synchronize_session=False)
                    db.flush()
                    if affected == 0:
                        self._registry.mark_lost(claim.job_id, claim.claim_token)
                db.commit()
            except Exception:
                logger.exception("Heartbeat renewal failed (non-fatal)")
            finally:
                if db is not None:
                    db.close()
