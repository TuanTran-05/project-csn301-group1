"""Resolve which project a request is acting on.

Every route that touches devices, changes, chat, monitoring, audit or the
dashboard calls ``current_project()`` first and passes the result's id down.
The project is named by the ``X-Project-Id`` header (or a ``project_id``
query parameter). When the caller names none and has exactly one accessible
project, that one is used, so single-project clients keep working.
"""

from flask import g, request

from ..auth.service import current_user
from ..errors import ProjectRequiredError, ValidationError
from . import service
from .model import Project


def _requested_project_id() -> int | None:
    raw = request.headers.get("X-Project-Id")
    if raw is None or raw == "":
        raw = request.args.get("project_id")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("project id must be an integer.") from exc


def current_project(need: str = "viewer") -> Project:
    """The project this request acts on, checked for ``need`` access.

    Requires a verified JWT (the route's own decorator provides it).
    """
    user = current_user()
    requested = _requested_project_id()

    if requested is not None:
        project = service.require_project(user, requested, need)
    else:
        projects = service.accessible_projects(user)
        if not projects:
            raise ProjectRequiredError(
                "You do not have access to any project yet. Create one first."
            )
        if len(projects) > 1:
            raise ProjectRequiredError(
                "Several projects are available; choose one with the "
                "X-Project-Id header.",
                {"project_ids": [project.id for project in projects]},
            )
        project = service.require_project(user, projects[0].id, need)

    # Lets audit.record_event tag every event of this request with the project.
    g.project_id = project.id
    return project


def write_project() -> Project:
    """The current project, for a route that changes inventory or design.

    Needs edit rights on the project and a role that is not read-only.
    """
    return current_project(need="editor")
