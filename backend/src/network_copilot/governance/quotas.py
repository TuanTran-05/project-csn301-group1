from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..errors import RateLimitError
from ..extensions import db
from .access import actor, policy, require
from .model import Usage, now


def consume_ai(user_id, group_id=None):
    user = actor(user_id)
    require(user, "ai.use", group_id)
    day = now().date().isoformat()
    if db.session.get(Usage, (user.id, day)) is None:
        try:
            with db.session.begin_nested():
                db.session.add(Usage(user_id=user.id, day=day, ai_calls=0))
                db.session.flush()
        except IntegrityError:
            pass
    result = db.session.execute(update(Usage).where(Usage.user_id == user.id, Usage.day == day,
                                                   Usage.ai_calls < policy().ai_daily_limit).values(ai_calls=Usage.ai_calls + 1))
    if result.rowcount != 1:
        db.session.rollback()
        raise RateLimitError("Daily AI quota reached.")
    db.session.commit()
