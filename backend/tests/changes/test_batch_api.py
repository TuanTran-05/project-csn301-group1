import pytest

from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation


def make_write_batch(user_id, devices):
    return batch_service.create_batch_preview(
        user_id,
        [BatchOperation(
            [device.hostname for device in devices],
            "exec",
            ["write memory"],
            [],
        )],
        "Save configurations",
    )


@pytest.fixture
def batch(app, admin_user, access_switch):
    return batch_service.create_batch_preview(
        admin_user.id,
        [BatchOperation([access_switch.hostname], "config", ["hostname NEW-SW"], [])],
        "Rename switch",
    )


@pytest.fixture
def approved_write_batch(app, admin_user, access_switch, dist_switch, ssh_factory):
    batch = make_write_batch(admin_user.id, [access_switch, dist_switch])
    for device in (access_switch, dist_switch):
        ssh_factory.set_client(
            device.hostname,
            responses={
                "show running-config": f"hostname {device.hostname}",
                "show startup-config": f"hostname {device.hostname}",
            },
        )
    return batch_service.approve_batch(batch.id, admin_user.id)


def test_batch_get_requires_authentication(client, batch):
    assert client.get(f"/api/change-batches/{batch.id}").status_code == 401


def test_batch_list_requires_authentication(client):
    assert client.get("/api/change-batches").status_code == 401


def test_authenticated_user_can_list_and_get_batches(
    client, viewer_headers, batch
):
    list_response = client.get("/api/change-batches", headers=viewer_headers)
    get_response = client.get(
        f"/api/change-batches/{batch.id}", headers=viewer_headers
    )

    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.get_json()["items"]] == [batch.id]
    assert get_response.status_code == 200
    assert get_response.get_json()["id"] == batch.id


@pytest.mark.parametrize("suffix", ["approve", "apply", "cancel"])
def test_batch_mutation_requires_admin(client, viewer_headers, batch, suffix):
    response = client.post(
        f"/api/change-batches/{batch.id}/{suffix}", headers=viewer_headers, json={}
    )
    assert response.status_code == 403


def test_batch_approve_returns_updated_batch(client, admin_headers, batch):
    response = client.post(
        f"/api/change-batches/{batch.id}/approve", headers=admin_headers, json={}
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "approved"


def test_batch_cancel_returns_updated_batch(client, admin_headers, batch):
    response = client.post(
        f"/api/change-batches/{batch.id}/cancel", headers=admin_headers, json={}
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "cancelled"


def test_batch_apply_accepts_confirmation_field(client, admin_headers, approved_write_batch):
    response = client.post(
        f"/api/change-batches/{approved_write_batch.id}/apply",
        headers=admin_headers,
        json={"confirmation": "CONFIRM ALL"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] in {"success", "partial_success", "failed"}


@pytest.mark.parametrize("payload", ["CONFIRM ALL", 1, ["CONFIRM ALL"]])
def test_batch_apply_rejects_non_object_json(
    client, admin_headers, approved_write_batch, payload
):
    response = client.post(
        f"/api/change-batches/{approved_write_batch.id}/apply",
        headers=admin_headers,
        json=payload,
    )

    assert response.status_code == 422
    assert response.get_json()["error"] == "validation_error"


def test_standalone_filter_excludes_batch_children(client, admin_headers, batch):
    body = client.get(
        "/api/changes?standalone_only=true&limit=500", headers=admin_headers
    ).get_json()
    assert all(item["batch_id"] is None for item in body["items"])


def test_standalone_filter_false_preserves_default_results(
    client, admin_headers, batch
):
    default_body = client.get("/api/changes?limit=500", headers=admin_headers).get_json()
    false_body = client.get(
        "/api/changes?standalone_only=false&limit=500", headers=admin_headers
    ).get_json()

    assert [item["id"] for item in false_body["items"]] == [
        item["id"] for item in default_body["items"]
    ]
    assert any(item["batch_id"] == batch.id for item in false_body["items"])


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE", "False"])
def test_standalone_filter_rejects_non_strict_boolean_values(
    client, admin_headers, value
):
    response = client.get(
        f"/api/changes?standalone_only={value}", headers=admin_headers
    )

    assert response.status_code == 422
    assert response.get_json()["error"] == "validation_error"
