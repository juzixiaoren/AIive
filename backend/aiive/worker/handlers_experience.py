"""Experience capture uses the existing fenced Outbox transport."""
from sqlalchemy.exc import OperationalError

from aiive.db.base import SessionLocal
from aiive.experience.capture import build_episode
from aiive.worker.job_source_guard import guard_job_sources_visible
from aiive.worker.outbox_dto import ClaimedJob, HandlerOutcome, HandlerResult
from aiive.worker.outbox_execution_auth import with_execution_authorization


@with_execution_authorization
def handle_experience_capture(claimed: ClaimedJob) -> HandlerResult:
    fresh, blocked = guard_job_sources_visible(claimed, SessionLocal)
    if blocked is not None:
        return blocked
    assert fresh is not None
    db = SessionLocal()
    try:
        episode = build_episode(db, fresh.payload)
        db.commit()
        return HandlerResult(HandlerOutcome.COMPLETED, episode.id if episode else "source_unavailable")
    except ValueError:
        db.rollback()
        return HandlerResult(HandlerOutcome.COMPLETED, "invalid_capture_source")
    except OperationalError as exc:
        db.rollback()
        return HandlerResult(HandlerOutcome.RETRYABLE_ERROR, type(exc).__name__)
    finally:
        db.close()
