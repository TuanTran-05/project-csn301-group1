"""Application level exceptions and the shared JSON error contract."""


class AppError(Exception):
    """Base class for errors that map onto a JSON HTTP response."""

    status_code = 500
    error = "internal_error"

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"error": self.error, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(AppError):
    status_code = 422
    error = "validation_error"


class NotFoundError(AppError):
    status_code = 404
    error = "not_found"


class ConflictError(AppError):
    status_code = 409
    error = "conflict"


class ForbiddenError(AppError):
    status_code = 403
    error = "forbidden"


class PolicyViolationError(AppError):
    """Raised when the command policy engine blocks a command."""

    status_code = 403
    error = "policy_violation"


class DeviceConnectionError(AppError):
    status_code = 502
    error = "device_unreachable"


class InvalidStateError(AppError):
    status_code = 409
    error = "invalid_state"


class RateLimitError(AppError):
    status_code = 429
    error = "rate_limit_exceeded"


# Maps a bare HTTP status onto the shared JSON error contract.
HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limit_exceeded",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
}


def error_payload(status_code: int, message: str, details: dict | None = None) -> dict:
    payload = {
        "error": HTTP_ERROR_CODES.get(status_code, "error"),
        "message": message,
    }
    if details:
        payload["details"] = details
    return payload
