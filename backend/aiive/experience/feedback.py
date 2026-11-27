"""Conservative follow-up attribution within a conversation."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from aiive.db.models import Event, ExperienceEpisode, ExperienceSignal, TurnRecord
from aiive.experience.capture import add_signal, episode_visible, turn_visible, utc
from aiive.experience.families import family_for

FOLLOW_UP_WINDOW = timedelta(days=2)
_BATCH_SIZE = 100

_NEW = re.compile(
    r"^(?:另一个|另外(?:一个)?|换个话题|新问题|新增需求|接下来想|顺便问|还有件事|换个任务|"
    + r"new (?:question|task|topic)\b|separately\b|unrelated\b|switching topics\b)", re.I,
)
_REWORK = re.compile(
    r"^(?:请(?:你)?)?(?:重做|重新(?:做|生成|回答|写|整理)|再做一遍|返工|按.*重来|"
    + r"(?:please )?(?:redo|try again|rewrite it|start over)\b)", re.I,
)
_CORRECTION = re.compile(
    r"^(?:不对|错了|你(?:理解错|弄错|搞错|漏了|写错|答错|忘了|误解)|"
    + r"我(?:刚才)?(?:是说|的意思是)|应该是|"
    + r"(?:that's|that is) (?:not what I|wrong)|you (?:missed|misunderstood|got .* wrong)|"
    + r"I (?:meant|asked for)\b|correction\b)|^(?:我(?:要|说的)的是|I asked for).{1,100}(?:不是|not\b)", re.I,
)
_POSITIVE = re.compile(r"^(?:这个回答(?:很)?有帮助|解决了我的问题|结果正确|that (?:solved it|was helpful)|this answers my question)[。.!！\s]*$", re.I)
_CONTINUATION = re.compile(r"^(?:继续|展开|具体|再详细|能解释|what about|continue|elaborate|tell me more)\b", re.I)


def classify_follow_up(message: str, previous_request: str) -> str:
    text = message.strip()
    if _NEW.search(text):
        return "new_request"
    if _REWORK.search(text):
        return "rework"
    if _CORRECTION.search(text):
        return "corrected"
    if _POSITIVE.fullmatch(text):
        return "explicit_positive"
    if _CONTINUATION.search(text) or text.startswith(("继续", "展开", "具体", "能解释")):
        return "continuation"
    family = family_for(text)
    if family != "general" and family_for(previous_request) != family:
        return "new_request"
    return "continuation"


def analyze_follow_ups(db: Session, now: datetime) -> int:
    """Process bounded batches. A message cannot jump over an intervening user turn."""
    seen = exists().where(
        ExperienceSignal.source_id == Event.id,
        ExperienceSignal.kind.in_(("new_request", "continuation", "explicit_positive", "corrected", "rework")),
    )
    previous, current, following = (aliased(TurnRecord) for _ in range(3))
    next_sequence = select(func.min(following.turn_sequence)).where(
        following.thread_id == previous.thread_id, following.source == "user",
        following.turn_sequence > previous.turn_sequence,
    ).correlate(previous).scalar_subquery()
    rows = db.query(ExperienceEpisode, previous, current, Event).join(
        previous, previous.id == ExperienceEpisode.source_id,
    ).join(current, (current.thread_id == previous.thread_id) & (current.source == "user")
           & (current.turn_sequence == next_sequence)).join(
        Event, current.request_event_id == Event.id,
    ).filter(
        ExperienceEpisode.kind == "turn", Event.event_type == "user_message",
        Event.created_at >= now - FOLLOW_UP_WINDOW * 3, Event.created_at <= now, ~seen,
    ).order_by(Event.created_at, Event.id).limit(_BATCH_SIZE * 5).all()
    processed = 0
    for row, previous_turn, current_turn, message in rows:
        if not turn_visible(db, current_turn):
            continue
        episode = db.query(ExperienceEpisode).filter_by(id=row.id).with_for_update().one_or_none()
        if episode is None or not episode_visible(db, episode):
            continue
        if not utc(previous_turn.updated_at) <= utc(message.created_at) <= utc(previous_turn.updated_at) + FOLLOW_UP_WINDOW:
            continue
        request = db.get(Event, previous_turn.request_event_id) if previous_turn.request_event_id else None
        if request is None:
            continue
        kind = classify_follow_up(str(message.payload.get("content") or ""), str(request.payload.get("content") or ""))
        signal = add_signal(
            db, episode, kind, message.id, {"source_type": "event", "method": "follow_up"},
            identity=f"follow_up:{message.id}", created_at=message.created_at,
        )
        if signal in db.new and kind in {"corrected", "rework", "explicit_positive"}:
            episode.reflected_at = None
        processed += 1
        if processed >= _BATCH_SIZE:
            break
    return processed


def record_feedback(db: Session, episode: ExperienceEpisode, kind: str) -> ExperienceSignal:
    signal = add_signal(
        db, episode, kind, episode.id, {"method": "explicit"},
        identity=f"explicit:{episode.id}",
    )
    episode.reflected_at = None
    return signal
