"""Project CRUD, sharing and the access rules everything else relies on."""

import ipaddress

from pydantic import ValidationError as PydanticValidationError

from ..auth.model import User
from ..errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from ..extensions import db
from .model import Project, ProjectMember
from .schemas import (
    MemberSchema,
    MemberUpdateSchema,
    ProjectCreateSchema,
    ProjectUpdateSchema,
)

ACCESS_RANK = {"viewer": 1, "editor": 2, "owner": 3}

_ACTIVE_BATCH_STATUSES = ("pending_approval", "approved", "running")


def validate(schema, payload: dict, what: str = "Project"):
    try:
        return schema.model_validate(payload)
    except PydanticValidationError as exc:
        details: dict[str, list[str]] = {}
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "_root"
            details.setdefault(field, []).append(error["msg"])
        raise ValidationError(f"{what} payload failed validation.", details) from exc


# -- access -----------------------------------------------------------------


def access_level(user: User | None, project: Project) -> str | None:
    """The caller's effective access to a project, or None for no access.

    ADMIN always gets full access. A global VIEWER is read-only everywhere,
    even on a project they own or were granted edit rights on.
    """
    if user is None or not user.is_active:
        return None
    if user.role == "ADMIN":
        return "owner"

    if project.owner_id == user.id:
        level = "owner"
    else:
        member = (
            db.session.query(ProjectMember)
            .filter_by(project_id=project.id, user_id=user.id)
            .one_or_none()
        )
        level = member.access if member else None
    if level is None:
        return None
    return "viewer" if user.role == "VIEWER" else level


def accessible_projects(user: User | None) -> list[Project]:
    if user is None or not user.is_active:
        return []
    query = db.session.query(Project)
    if user.role != "ADMIN":
        shared = db.session.query(ProjectMember.project_id).filter(
            ProjectMember.user_id == user.id
        )
        query = query.filter(
            db.or_(Project.owner_id == user.id, Project.id.in_(shared))
        )
    return query.order_by(Project.name, Project.id).all()


def require_project(user: User | None, project_id: int, need: str = "viewer") -> Project:
    """Load a project the caller may use at ``need`` level or above.

    A project the caller cannot see at all is reported as not found, so
    project ids cannot be probed.
    """
    project = db.session.get(Project, project_id)
    level = access_level(user, project) if project is not None else None
    if level is None:
        raise NotFoundError(f"Project {project_id} was not found.")
    if ACCESS_RANK[level] < ACCESS_RANK[need]:
        raise ForbiddenError(
            f"This action needs {need} access to the project; you have {level}."
        )
    return project


def project_view(
    user: User | None,
    project: Project,
    device_counts: dict | None = None,
    owners: dict | None = None,
) -> dict:
    """A project as the API shows it.

    ``device_counts`` / ``owners`` let a listing pass pre-fetched lookups so it
    costs two queries in total rather than two per project.
    """
    from ..devices.model import Device

    data = project.to_dict()
    data["access"] = access_level(user, project)
    if device_counts is not None:
        data["device_count"] = device_counts.get(project.id, 0)
    else:
        data["device_count"] = (
            db.session.query(db.func.count(Device.id))
            .filter(Device.project_id == project.id)
            .scalar()
        )
    if owners is not None:
        data["owner"] = owners.get(project.owner_id)
    else:
        owner = db.session.get(User, project.owner_id) if project.owner_id else None
        data["owner"] = owner.username if owner else None
    return data


def project_views(user: User | None, projects: list[Project]) -> list[dict]:
    from ..devices.model import Device

    ids = [project.id for project in projects]
    counts = dict(
        db.session.query(Device.project_id, db.func.count(Device.id))
        .filter(Device.project_id.in_(ids))
        .group_by(Device.project_id)
        .all()
    ) if ids else {}
    owner_ids = {p.owner_id for p in projects if p.owner_id}
    owners = dict(
        db.session.query(User.id, User.username).filter(User.id.in_(owner_ids)).all()
    ) if owner_ids else {}
    return [project_view(user, p, counts, owners) for p in projects]


# -- CRUD -------------------------------------------------------------------


def create_project(user: User, payload: dict) -> Project:
    if user.role == "VIEWER":
        raise ForbiddenError("A read-only account cannot create projects.")
    data = validate(ProjectCreateSchema, payload)
    existing = (
        db.session.query(Project)
        .filter(Project.owner_id == user.id, Project.name == data.name)
        .first()
    )
    if existing is not None:
        raise ConflictError(f"You already have a project named {data.name}.")
    project = Project(owner_id=user.id, **data.model_dump())
    db.session.add(project)
    db.session.commit()
    return project


def update_project(project: Project, payload: dict) -> Project:
    data = validate(ProjectUpdateSchema, payload)
    changes = data.model_dump(exclude_unset=True)

    new_network = changes.get("management_network")
    if new_network and new_network != project.management_network:
        _assert_devices_fit(project, ipaddress.ip_network(new_network))

    new_name = changes.get("name")
    if new_name and new_name != project.name:
        clash = (
            db.session.query(Project)
            .filter(
                Project.owner_id == project.owner_id,
                Project.name == new_name,
                Project.id != project.id,
            )
            .first()
        )
        if clash is not None:
            raise ConflictError(f"A project named {new_name} already exists.")

    for field, value in changes.items():
        setattr(project, field, value)
    db.session.commit()
    return project


def _assert_devices_fit(project: Project, network) -> None:
    from ..devices.model import Device

    outside = [
        device.hostname
        for device in db.session.query(Device).filter(Device.project_id == project.id)
        if ipaddress.ip_address(device.management_ip) not in network
    ]
    if outside:
        raise ConflictError(
            f"Devices outside {network} must be moved or re-addressed first.",
            {"devices": sorted(outside)},
        )


def delete_project(project: Project) -> None:
    from ..changes.model import ChangeBatch
    from ..chat.model import ChatMessage, ChatSession
    from ..devices.model import Device
    from ..devices.service import purge_devices
    from ..topology.model import TopologyLink

    active = (
        db.session.query(ChangeBatch)
        .filter(
            ChangeBatch.project_id == project.id,
            ChangeBatch.status.in_(_ACTIVE_BATCH_STATUSES),
        )
        .first()
    )
    if active is not None:
        raise ConflictError(
            "The project has an active change batch and cannot be deleted."
        )

    device_ids = [
        row[0]
        for row in db.session.query(Device.id).filter(Device.project_id == project.id)
    ]
    purge_devices(device_ids, commit=False)

    for batch in db.session.query(ChangeBatch).filter(
        ChangeBatch.project_id == project.id
    ):
        db.session.delete(batch)
    session_ids = [
        row[0]
        for row in db.session.query(ChatSession.id).filter(
            ChatSession.project_id == project.id
        )
    ]
    if session_ids:
        db.session.query(ChatMessage).filter(
            ChatMessage.session_id.in_(session_ids)
        ).delete(synchronize_session=False)
        db.session.query(ChatSession).filter(ChatSession.id.in_(session_ids)).delete(
            synchronize_session=False
        )
    db.session.query(TopologyLink).filter(
        TopologyLink.project_id == project.id
    ).delete(synchronize_session=False)
    db.session.query(ProjectMember).filter(
        ProjectMember.project_id == project.id
    ).delete(synchronize_session=False)
    db.session.delete(project)
    db.session.commit()


# -- sharing ----------------------------------------------------------------


def list_members(project: Project) -> list[ProjectMember]:
    return (
        db.session.query(ProjectMember)
        .filter(ProjectMember.project_id == project.id)
        .join(User, User.id == ProjectMember.user_id)
        .order_by(User.username)
        .all()
    )


def share_project(project: Project, granted_by: User, payload: dict) -> ProjectMember:
    data = validate(MemberSchema, payload, "Share")
    target = db.session.query(User).filter_by(username=data.username).one_or_none()
    if target is None or not target.is_active:
        raise NotFoundError(f"User {data.username} was not found.")
    if target.id == project.owner_id:
        raise ConflictError("The owner already has full access to the project.")

    member = (
        db.session.query(ProjectMember)
        .filter_by(project_id=project.id, user_id=target.id)
        .one_or_none()
    )
    if member is None:
        member = ProjectMember(
            project_id=project.id, user_id=target.id, granted_by_id=granted_by.id
        )
        db.session.add(member)
    member.access = data.access
    db.session.commit()
    return member


def update_member(project: Project, user_id: int, payload: dict) -> ProjectMember:
    data = validate(MemberUpdateSchema, payload, "Share")
    member = _get_member(project, user_id)
    member.access = data.access
    db.session.commit()
    return member


def remove_member(project: Project, user_id: int) -> None:
    member = _get_member(project, user_id)
    db.session.delete(member)
    db.session.commit()


def _get_member(project: Project, user_id: int) -> ProjectMember:
    member = (
        db.session.query(ProjectMember)
        .filter_by(project_id=project.id, user_id=user_id)
        .one_or_none()
    )
    if member is None:
        raise NotFoundError("That user does not have access to this project.")
    return member
