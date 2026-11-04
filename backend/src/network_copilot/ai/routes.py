import logging
import re

from flask import Blueprint, current_app, g, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pydantic import ValidationError as PydanticValidationError

from ..auth.model import User
from ..auth.service import current_user
from ..chat.service import record_message as record_chat_message
from ..chat.session_service import create_session, resolve_or_create_session
from ..chat.onboarding import (
    requests_network_design,
    requests_project,
    requested_device_count,
    topology_options,
)
from ..errors import ForbiddenError, ValidationError
from ..extensions import db, limiter
from ..projects.scope import current_project
from ..projects import service as project_service
from ..projects.model import Project
from ..audit.service import record_event
from . import design_assistant, intent
from .schemas import MAX_CHAT_MESSAGE_LENGTH, ChatRequest
from .service import SCHEMA_MISMATCH, AIService

logger = logging.getLogger(__name__)

bp = Blueprint("ai", __name__, url_prefix="/api/ai")


@bp.after_app_request
def record_failed_chat_response(response):
    """Store one safe transcript entry for each authenticated chat failure."""
    if request.endpoint != "ai.chat" or 200 <= response.status_code < 300:
        return response

    try:
        identity = get_jwt_identity()
    except RuntimeError:
        # JWT verification did not complete, so this is an unauthenticated 401.
        return response
    if identity is None:
        return response

    # No project was resolved (none chosen, or no access): there is no
    # transcript to put the failure in.
    project_id = getattr(g, "project_id", None)
    if project_id is None:
        return response

    user = current_user()
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {"error": "request_failed", "message": "Request failed."}
    content = payload.get("message")
    if not isinstance(content, str):
        content = "Request failed."
    # Set by chat() once it resolved the caller's own session. A request that
    # failed before that goes to the caller's latest session, never to a
    # session id taken from the body, which may be someone else's.
    session_id = getattr(g, "chat_session_id", None)
    if session_id is None:
        session_id = resolve_or_create_session(None, project_id, user, write=True).id
    record_chat_message(
        user.id if user else None,
        user.username if user else None,
        "system",
        content,
        payload,
        session_id=session_id,
        project_id=project_id,
    )
    return response


def _new_project_name(user: User) -> str:
    prefix = f"{user.username} Lab"
    names = {
        project.name
        for project in db.session.query(Project).filter(Project.owner_id == user.id)
    }
    number = 1
    while f"{prefix} {number}" in names:
        number += 1
    return f"{prefix} {number}"


def _chat_reply(user, session_id, project_id, content, payload):
    saved = record_chat_message(
        user.id, user.username, "assistant", content, payload,
        session_id=session_id, project_id=project_id,
    )
    return jsonify({"intent": payload.get("flow", "advice"), "explanation": content,
                    "session_id": session_id,
                    "chat_message_id": saved.id if saved else None,
                    **payload}), 200


# A follow-up that reads or targets a real device leaves the design thread.
# Anywhere in the (accent-folded) message: a CLI read command, or a verb that
# asks to run or check something on a device ("chạy show run trên sw1").
_DEVICE_COMMAND = re.compile(
    r"(?<![\w-])(?:show|sh|ping|traceroute|tracert|debug|chay|run|thuc\s+thi|execute|"
    r"kiem\s+tra|check|verify)(?![\w-])"
)


# A message that starts as a CLI command is a read on a device: no need to ask.
_CLI_LINE = re.compile(r"^\s*(?:show|sh|ping|traceroute|tracert)\b", re.IGNORECASE)


def _wants_design(message: str, session_id: int, project_id: int) -> bool:
    """Whether the message is for the design advisor (only asked for members who may design).

    The model decides (ai/intent.py) when it is on and answers; otherwise the
    keyword rules do: a design request, or a follow-up of the design just suggested.
    """
    if intent.enabled() and not _CLI_LINE.match(message):
        route = intent.classify(message, session_id, project_id, _after_design(session_id))
        if route is not None:
            g.chat_route = route.intent
            logger.info("Chat message routed as %s %s", route.intent, route.devices or "")
            return route.intent == "design"
    return requests_network_design(message) or _continues_design(session_id, project_id, message)


def _after_design(session_id: int) -> bool:
    """Whether the session's last assistant reply is a design suggestion."""
    from ..chat.model import ChatMessage

    last = (
        db.session.query(ChatMessage)
        .filter_by(session_id=session_id, role="assistant")
        .order_by(ChatMessage.id.desc())
        .first()
    )
    return last is not None and (last.payload or {}).get("intent") == "advice"


def _continues_design(session_id: int, project_id: int, message: str) -> bool:
    """Whether ``message`` follows up the design the assistant just proposed.

    "đặt dải ip là 172.20.5.0/24" after a suggested diagram changes that
    diagram; sent to the device-action prompt it fails its schema. A message
    that names a device of the project, or is a show/ping command, is about
    the real network and keeps going there.
    """
    from ..chat.onboarding import _fold
    from ..devices.model import Device

    if not _after_design(session_id):
        return False

    text = _fold(message)
    if _DEVICE_COMMAND.search(text):
        return False
    return not any(
        re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)
        for (hostname,) in db.session.query(Device.hostname).filter(Device.project_id == project_id)
        if hostname
        for name in _device_names(hostname)
    )


def _device_names(hostname: str) -> set[str]:
    """What a member may call a device: SWITCH-SW1 is also "sw1" (its last part)."""
    name = hostname.casefold()
    names = {name}
    parts = [part for part in re.split(r"[-_.]", name) if part]
    if len(parts) > 1 and re.search(r"\d", parts[-1]):
        names.add(parts[-1])
    return names


@bp.post("/chat")
@jwt_required()
@limiter.limit("20 per minute")
def chat():
    project = current_project()
    try:
        data = ChatRequest.model_validate(request.get_json(silent=True) or {})
    except PydanticValidationError as exc:
        details: dict[str, list[str]] = {}
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "_root"
            details.setdefault(field, []).append(error["msg"])
        if any(error["type"] == "string_too_long" for error in exc.errors()):
            raise ValidationError(
                f"Tin nhắn quá dài: tối đa {MAX_CHAT_MESSAGE_LENGTH} ký tự. "
                "Hãy chia yêu cầu thành nhiều bước.",
                details,
            ) from exc
        raise ValidationError("A non-empty 'message' is required.", details) from exc

    user = current_user()
    user_id = user.id if user else None
    username = user.username if user else None
    project_read_only = project_service.access_level(user, project) == "viewer"
    # Chat sessions are shared by the project; resolve a missing or foreign
    # project session to the latest session the caller may access.
    data.session_id = resolve_or_create_session(
        data.session_id, project.id, user, write=True
    ).id
    g.chat_session_id = data.session_id

    # Only what creates something is refused here. Merely mentioning the topology
    # must not block a diagnostic; a configure intent is refused by AIService.
    if project_read_only and (data.mode == "design" or requests_project(data.message)):
        raise ForbiddenError(
            "Quyền Viewer trong project chỉ cho phép lệnh chỉ-đọc như show, ping, "
            "traceroute; "
            "không thể tạo project, topology hoặc cấu hình thiết bị."
        )

    if (
        user.role == "OPERATOR"
        and requests_project(data.message)
        and not requests_network_design(data.message)
    ):
        created = project_service.create_project(user, {
            "name": _new_project_name(user),
            "description": "Project được khởi tạo từ khung chat.",
            "management_network": current_app.config["MANAGEMENT_NETWORK"],
            "environment": "pnetlab",
        })
        session = create_session(created.id, created_by_id=user.id)
        record_chat_message(user_id, username, "user", data.message,
                            session_id=session.id, project_id=created.id)
        record_event(action="project.create", result="success", user_id=user.id,
                     project_id=created.id, details={"source": "chat"})
        content = "Bạn muốn project có bao nhiêu thiết bị mạng? Hãy nhập một số từ 2 đến 25."
        payload = {"flow": "topology_setup_count", "project_id": created.id,
                   "project": project_service.project_view(user, created)}
        record_chat_message(user_id, username, "assistant", content, payload,
                            session_id=session.id, project_id=created.id)
        return jsonify({"intent": "project_created", "explanation": content,
                        "session_id": session.id, **payload}), 201

    if user.role == "OPERATOR" and not project_read_only:
        from ..chat.model import ChatMessage, ChatSession
        prompt = db.session.query(ChatMessage).filter_by(
            session_id=data.session_id, role="assistant"
        ).join(ChatSession).filter(
            ChatSession.project_id == project.id
        ).order_by(ChatMessage.id.desc()).first()
        if prompt and (prompt.payload or {}).get("flow") == "topology_setup_count":
            count = requested_device_count(data.message)
            record_chat_message(user_id, username, "user", data.message,
                                session_id=data.session_id, project_id=project.id)
            if count is None:
                content = "Nhập số thiết bị mạng từ 2 đến 25 để mình tạo các phương án topology."
                return _chat_reply(user, data.session_id, project.id, content,
                                   {"flow": "topology_setup_count", "project_id": project.id})
            options = topology_options(count)
            content = f"Mình đã chuẩn bị {len(options)} phương án cho {count} thiết bị mạng. Chọn một sơ đồ để tạo rồi chỉnh sửa trên canvas."
            return _chat_reply(user, data.session_id, project.id, content,
                               {"flow": "topology_options", "project_id": project.id,
                                "device_count": count, "options": options})

    record_chat_message(
        user_id, username, "user", data.message,
        session_id=data.session_id, project_id=project.id,
    )
    if data.mode == "design":
        service = AIService()
        result = design_assistant.propose(
            service.provider, data.message, project,
            service._recent_history(data.session_id, data.message, project.id),
        )
    elif (typed := AIService.running_config_request(
        data.message, user_id, project.id, data.group_id
    )) is not None:
        result = typed
    elif not project_read_only and user.role != "VIEWER" and _wants_design(
        data.message, data.session_id, project.id
    ):
        result = AIService().advise(data.message, data.session_id, project.id)
    else:
        try:
            result = AIService().handle(
                data.message, user_id, session_id=data.session_id, project_id=project.id,
                group_id=data.group_id,
            )
        except ValidationError as exc:
            # A design follow-up that still reached the device prompt: the model
            # answered with a design, which that schema refuses. Ask the advisor.
            if not (exc.message == SCHEMA_MISMATCH and not project_read_only
                    and user.role != "VIEWER" and _after_design(data.session_id)):
                raise
            result = AIService().advise(data.message, data.session_id, project.id)
    if isinstance(result.get("diagram"), dict):
        from ..topology.chat_import import complete_diagram_addresses

        result["diagram"], generated_addresses = complete_diagram_addresses(
            project, result["diagram"]
        )
        if generated_addresses:
            pool = result.get("addressing_pool") or project.addressing_pool or "10.100.0.0/16"
            result["explanation"] = (
                result.get("explanation", "")
                + f"\n\nCác địa chỉ IP còn thiếu được đề xuất từ dải {pool}; "
                "chưa được áp dụng lên thiết bị."
            )
    saved = record_chat_message(
        user_id, username, "assistant", result.get("explanation", ""), result,
        session_id=data.session_id, project_id=project.id,
    )
    return jsonify({**result, "session_id": data.session_id,
                    "chat_message_id": saved.id if saved else None}), 200
