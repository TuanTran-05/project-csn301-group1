from ..audit.service import record_event
from ..auth.model import User
from ..errors import ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from .model import Group


def set_auto_approve(group_id: int, user_id: int | None, enabled: bool) -> Group:
    if not isinstance(enabled, bool):
        raise ValidationError("is_auto_approve must be a boolean.")

    actor = db.session.get(User, user_id) if user_id is not None else None
    if actor is None or not actor.is_active:
        raise ForbiddenError("An active authenticated user is required.")

    group = db.session.get(Group, group_id)
    if group is None:
        raise NotFoundError(f"Group {group_id} was not found.")
    from ..governance.access import require, require_group
    require_group(actor, group_id, manage=True)
    require(actor, "config.approve", group_id)

    role = actor.role.value if hasattr(actor.role, "value") else str(actor.role)
    if role == "TEACHER":
        if group not in actor.teaching_groups:
            raise ForbiddenError("Teachers may only manage their assigned groups.")
    elif role != "ADMIN":
        raise ForbiddenError(
            "Only an Admin or the assigned Teacher may change this setting."
        )

    group.is_auto_approve = enabled
    db.session.commit()
    record_event(
        action="group.auto_approve",
        result="success",
        user_id=actor.id,
        details={"group_id": group.id, "is_auto_approve": enabled},
    )
    return group
