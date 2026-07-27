from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from ..errors import NotFoundError
from ..extensions import db
from .model import DeviceCredential


class CredentialCipher:
    """Symmetric encryption for device secrets, backed by Fernet."""

    def __init__(self, key: str | bytes | None):
        if not key:
            raise ValueError(
                "A credential encryption key is required. "
                "Set CREDENTIAL_ENCRYPTION_KEY in the environment."
            )
        if isinstance(key, str):
            key = key.encode()
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key."
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored credential could not be decrypted.") from exc


def get_cipher() -> CredentialCipher:
    """Build a cipher from the active app config."""
    from flask import current_app

    return CredentialCipher(current_app.config.get("CREDENTIAL_ENCRYPTION_KEY"))


@dataclass(frozen=True)
class PlainCredential:
    """Decrypted credential, used in-process only. Never serialise this."""

    username: str
    password: str
    enable_secret: str | None = None


def store_device_credential(
    device_id: int,
    username: str,
    password: str,
    enable_secret: str | None = None,
) -> DeviceCredential:
    cipher = get_cipher()
    record = (
        db.session.query(DeviceCredential)
        .filter_by(device_id=device_id)
        .one_or_none()
    )
    if record is None:
        record = DeviceCredential(device_id=device_id)
        db.session.add(record)

    record.username = username
    record.password_encrypted = cipher.encrypt(password)
    record.enable_secret_encrypted = (
        cipher.encrypt(enable_secret) if enable_secret else None
    )
    db.session.commit()
    return record


def get_device_credential(device_id: int) -> PlainCredential:
    record = (
        db.session.query(DeviceCredential)
        .filter_by(device_id=device_id)
        .one_or_none()
    )
    if record is None:
        raise NotFoundError(f"No credential is stored for device {device_id}.")

    cipher = get_cipher()
    return PlainCredential(
        username=record.username,
        password=cipher.decrypt(record.password_encrypted),
        enable_secret=(
            cipher.decrypt(record.enable_secret_encrypted)
            if record.enable_secret_encrypted
            else None
        ),
    )


def delete_device_credential(device_id: int) -> None:
    record = (
        db.session.query(DeviceCredential)
        .filter_by(device_id=device_id)
        .one_or_none()
    )
    if record is None:
        raise NotFoundError(f"No credential is stored for device {device_id}.")
    db.session.delete(record)
    db.session.commit()
