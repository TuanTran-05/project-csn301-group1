import pytest
from cryptography.fernet import Fernet

from network_copilot.credentials.service import (
    CredentialCipher,
    delete_device_credential,
    get_device_credential,
    store_device_credential,
)
from network_copilot.errors import NotFoundError
from network_copilot.extensions import db


@pytest.fixture
def test_key():
    return Fernet.generate_key().decode()


def test_credential_is_not_plaintext(test_key):
    cipher = CredentialCipher(test_key)
    token = cipher.encrypt("Secret123!")
    assert token != "Secret123!"
    assert "Secret123!" not in token
    assert cipher.decrypt(token) == "Secret123!"


def test_encrypt_is_non_deterministic(test_key):
    cipher = CredentialCipher(test_key)
    assert cipher.encrypt("Secret123!") != cipher.encrypt("Secret123!")


def test_decrypt_with_wrong_key_fails(test_key):
    token = CredentialCipher(test_key).encrypt("Secret123!")
    other = CredentialCipher(Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        other.decrypt(token)


def test_cipher_requires_a_key():
    with pytest.raises(ValueError):
        CredentialCipher(None)


def test_cipher_rejects_malformed_key():
    with pytest.raises(ValueError):
        CredentialCipher("not-a-valid-fernet-key")


def test_store_and_read_back_device_credential(app, device):
    store_device_credential(device.id, "ai-automation", "Secret123!")
    credential = get_device_credential(device.id)
    assert credential.username == "ai-automation"
    assert credential.password == "Secret123!"


def test_stored_password_is_encrypted_in_the_database(app, device):
    record = store_device_credential(device.id, "ai-automation", "Secret123!")
    db.session.expire_all()
    assert record.password_encrypted != "Secret123!"
    assert "Secret123!" not in record.password_encrypted


def test_credential_model_never_serialises_the_password(app, device):
    record = store_device_credential(device.id, "ai-automation", "Secret123!")
    serialised = record.to_dict()
    assert serialised["username"] == "ai-automation"
    assert "password" not in serialised
    assert "password_encrypted" not in serialised
    assert "Secret123!" not in str(serialised)


def test_store_replaces_the_existing_credential(app, device):
    store_device_credential(device.id, "old-user", "OldPass123!")
    store_device_credential(device.id, "new-user", "NewPass123!")
    credential = get_device_credential(device.id)
    assert credential.username == "new-user"
    assert credential.password == "NewPass123!"


def test_get_credential_missing_raises(app, device):
    with pytest.raises(NotFoundError):
        get_device_credential(device.id)


def test_delete_device_credential(app, device):
    store_device_credential(device.id, "ai-automation", "Secret123!")
    delete_device_credential(device.id)
    with pytest.raises(NotFoundError):
        get_device_credential(device.id)


def test_device_api_never_exposes_credentials(client, admin_headers, app, device):
    store_device_credential(device.id, "ai-automation", "Secret123!")
    body = client.get(f"/api/devices/{device.id}", headers=admin_headers).get_data(
        as_text=True
    )
    assert "Secret123!" not in body
    assert "ai-automation" not in body
    assert "password" not in body
