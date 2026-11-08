"""Replacing the project diagram with a chat diagram."""

from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.chat.service import record_message
from network_copilot.chat.session_service import create_session
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.topology.model import TopologyLink, TopologyVersion

LAB3_NAMES = {"EIU-R1", "EIU-S1", "PC1", "PC2", "PC3", "PC4", "PC5", "PC6"}


def _post_diagram(admin_user, project, nodes, links, title="Lab 3"):
    session = create_session(project.id, created_by_id=admin_user.id)
    return record_message(
        admin_user.id, admin_user.username, "assistant", "Sơ đồ gợi ý.",
        {"diagram": {"title": title, "nodes": nodes, "links": links}},
        session_id=session.id, project_id=project.id,
    )


def _old_message(admin_user, project):
    return _post_diagram(admin_user, project, [
        {"id": "r", "label": "R1", "kind": "router"},
        {"id": "s", "label": "SW1", "kind": "switch"},
    ], [{"source": "r", "target": "s"}], title="Old")


def _lab3_message(admin_user, project):
    nodes = [{"id": "r1", "label": "EIU-R1", "kind": "router"},
             {"id": "s1", "label": "EIU-S1", "kind": "switch"}] + [
        {"id": f"pc{n}", "label": f"PC{n}", "kind": "switch"} for n in range(1, 7)]
    links = [{"source": "r1", "target": "s1"}] + [{"source": "s1", "target": f"pc{n}"} for n in range(1, 7)]
    return _post_diagram(admin_user, project, nodes, links)


def _import(client, headers, base, message, **extra):
    return client.post(f"{base}/import/chat-diagram", headers=headers,
                       json={"message_id": message.id, **extra})


def _names(project):
    return {d.hostname for d in db.session.query(Device).filter_by(project_id=project.id)}


def test_replace_clears_the_old_diagram_and_saves_it_as_a_version(
    client, admin_headers, admin_user, project, base
):
    assert _import(client, admin_headers, base, _old_message(admin_user, project)).status_code == 201

    new = _lab3_message(admin_user, project)
    response = _import(client, admin_headers, base, new, replace=True)

    assert response.status_code == 201
    body = response.get_json()
    assert body["replaced"] == 2 and body["backup_id"]
    assert _names(project) == LAB3_NAMES   # no R1, no EIU-R1-2 copies
    assert db.session.query(TopologyLink).filter_by(project_id=project.id).count() == 7
    backup = db.session.get(TopologyVersion, body["backup_id"])
    assert backup.auto and backup.device_count == 2 and backup.link_count == 1

    # The same diagram can be put back again (start over).
    again = _import(client, admin_headers, base, new, replace=True)
    assert again.status_code == 201 and again.get_json()["replaced"] == 8
    assert _names(project) == LAB3_NAMES


def test_a_plain_import_still_adds_next_to_the_old_diagram(client, admin_headers, admin_user, project, base):
    _import(client, admin_headers, base, _old_message(admin_user, project))

    response = _import(client, admin_headers, base, _lab3_message(admin_user, project))

    assert response.status_code == 201 and response.get_json()["replaced"] == 0
    assert _names(project) == LAB3_NAMES | {"R1", "SW1"}


def test_replace_on_an_empty_project_is_a_plain_import(client, admin_headers, admin_user, project, base):
    response = _import(client, admin_headers, base, _lab3_message(admin_user, project), replace=True)

    assert response.status_code == 201
    assert response.get_json()["replaced"] == 0 and response.get_json()["backup_id"] is None


def test_replace_keeps_the_old_diagram_when_a_device_is_in_an_active_batch(
    client, admin_headers, admin_user, project, base
):
    devices = _import(client, admin_headers, base, _old_message(admin_user, project)).get_json()["devices"]
    target = db.session.get(Device, devices[0]["id"])
    target.management_ip = "172.16.3.50"   # a change request targets a reachable device
    batch = ChangeBatch(project_id=project.id, status="pending_approval", risk_level="low", source="ai")
    batch.changes = [ChangeRequest(device_id=target.id, commands=["hostname X"])]
    db.session.add(batch)
    db.session.commit()

    response = _import(client, admin_headers, base, _lab3_message(admin_user, project), replace=True)

    assert response.status_code == 409
    assert _names(project) == {"R1", "SW1"}
    assert db.session.query(TopologyLink).filter_by(project_id=project.id).count() == 1


def test_replace_must_be_a_boolean(client, admin_headers, admin_user, project, base):
    response = _import(client, admin_headers, base, _lab3_message(admin_user, project), replace="yes")
    assert response.status_code == 422
