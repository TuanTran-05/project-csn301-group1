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
