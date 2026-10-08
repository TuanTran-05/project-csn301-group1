import json

import pytest

from conftest import _create_device
from network_copilot.chat.service import record_message
from network_copilot.config import TestConfig
from network_copilot.devices import service as device_service
from network_copilot.extensions import db
from network_copilot.realtime import routes as realtime_routes
from network_copilot.realtime import state as project_state


@pytest.fixture(autouse=True)
def fast_streams(app):
    app.config.update(REALTIME_POLL_SECONDS=0.01, REALTIME_MAX_SECONDS=0.4, REALTIME_MAX_STREAMS=5)
    realtime_routes._open_streams = 0


def events(response):
    """Parse a finished event stream into (name, data) pairs."""
    text = b"".join(response.response).decode() if not isinstance(response.data, bytes) else response.get_data(as_text=True)
    out = []
    for block in text.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if name:
            out.append((name, data))
    return out


# -- fingerprints --------------------------------------------------------------------


def test_each_part_changes_only_when_its_data_changes(app, project, device):
    before = project_state.project_state(project.id)

    record_message(1, "g1", "user", "hello", project_id=project.id)
    after_message = project_state.project_state(project.id)
    assert after_message["messages"] != before["messages"]
    assert {k: v for k, v in after_message.items() if k != "messages"} == {k: v for k, v in before.items() if k != "messages"}

    device_service.set_device_status(device, "online")
    after_device = project_state.project_state(project.id)
    assert after_device["devices"] != after_message["devices"]
    assert after_device["messages"] == after_message["messages"]


def test_state_is_scoped_to_the_project(app, project):
    from network_copilot.projects.model import Project

    other = Project(name="Other", management_network="10.5.0.0/24")
    db.session.add(other)
    db.session.commit()
    before = project_state.project_state(project.id)
    _create_device("X1", "10.5.0.1", "core", project=other)
    record_message(1, "g1", "user", "elsewhere", project_id=other.id)
    assert project_state.project_state(project.id) == before


def test_change_status_moves_the_changes_fingerprint(client, admin_headers, project, device):
    created = client.post("/api/changes/preview", headers=admin_headers,
                          json={"device_id": device.id, "commands": ["vlan 20"]}).get_json()
    before = project_state.project_state(project.id)
    client.post(f"/api/changes/{created['id']}/approve", headers=admin_headers)
    after = project_state.project_state(project.id)
    assert after["changes"] != before["changes"]


# -- conditional polling ---------------------------------------------------------------


def test_state_endpoint_answers_304_when_nothing_changed(client, admin_headers, project):
    first = client.get(f"/api/projects/{project.id}/state", headers=admin_headers)
    assert first.status_code == 200 and set(first.get_json()["state"]) == {"messages", "changes", "batches", "devices"}
    etag = first.headers["ETag"]
    same = client.get(f"/api/projects/{project.id}/state", headers={**admin_headers, "If-None-Match": etag})
    assert same.status_code == 304
    record_message(1, "g1", "user", "new", project_id=project.id)
    changed = client.get(f"/api/projects/{project.id}/state", headers={**admin_headers, "If-None-Match": etag})
    assert changed.status_code == 200


def test_state_needs_project_access(client, admin_headers, viewer_headers, project):
    assert client.get(f"/api/projects/{project.id}/state", headers=viewer_headers).status_code == 200
    assert client.get("/api/projects/9999/state", headers=admin_headers).status_code == 404


# -- the event stream -------------------------------------------------------------------


def test_stream_sends_the_state_then_ends_with_bye(client, admin_headers, project):
    response = client.get(f"/api/projects/{project.id}/events", headers=admin_headers)
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    seen = events(response)
    assert seen[0][0] == "state" and set(seen[0][1]) == {"messages", "changes", "batches", "devices"}
    assert seen[-1] == ("bye", {"reconnect": True})
    assert realtime_routes._open_streams == 0  # the slot is released


def test_stream_announces_a_change_made_while_it_is_open(client, admin_headers, project):
    response = client.get(f"/api/projects/{project.id}/events", headers=admin_headers, buffered=False)
    stream = iter(response.response)
    first = next(stream).decode()
    assert first.startswith("event: state")
    record_message(1, "g1", "user", "while streaming", project_id=project.id)
    rest = b"".join(stream).decode()
    assert rest.count("event: state") == 1  # exactly one more, for the new message
    assert "event: bye" in rest


def test_stream_is_limited_and_can_be_switched_off(client, admin_headers, project, app):
    app.config["REALTIME_MAX_STREAMS"] = 0
    full = client.get(f"/api/projects/{project.id}/events", headers=admin_headers)
    assert full.status_code == 503 and full.headers["Retry-After"] == "30"
    app.config.update(REALTIME_MAX_STREAMS=5, REALTIME_ENABLED=False)
    off = client.get(f"/api/projects/{project.id}/events", headers=admin_headers)
    assert off.status_code == 503 and off.get_json()["error"] == "realtime_disabled"


def test_stream_needs_access_to_the_project(client, admin_headers, project):
    assert client.get("/api/projects/9999/events", headers=admin_headers).status_code == 404
    assert client.get(f"/api/projects/{project.id}/events").status_code == 401


def test_the_default_config_is_polite_about_threads():
    assert TestConfig.REALTIME_MAX_SECONDS <= 60 and TestConfig.REALTIME_MAX_STREAMS >= 1
