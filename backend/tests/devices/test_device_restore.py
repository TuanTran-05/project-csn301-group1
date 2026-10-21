from network_copilot.credentials.model import DeviceCredential
from network_copilot.credentials.service import get_device_credential, store_device_credential
from network_copilot.devices.model import Device
from network_copilot.extensions import db
from network_copilot.topology.model import DesignOspf, TopologyLink


def _lab(make_device):
    r1 = make_device("R1", "172.16.3.11", "core")
    sw1 = make_device("SW1", "172.16.3.21", "access")
    sw2 = make_device("SW2", "172.16.3.22", "access")
    store_device_credential(r1.id, "netops", "Secret123!")
    for b, port in ((sw1, "Gi0/1"), (sw2, "Gi0/2")):
        db.session.add(TopologyLink(
            project_id=r1.project_id, device_a_id=r1.id, interface_a=port,
            device_b_id=b.id, interface_b="Gi0/0", link_type="physical",
        ))
    db.session.add(DesignOspf(project_id=r1.project_id, device_id=r1.id, process_id=7))
    db.session.commit()
    return r1, sw1, sw2


def _delete(client, headers, device_id):
    response = client.delete(f"/api/devices/{device_id}?undo=1", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["restore_token"]


def _restore(client, headers, token):
    return client.post("/api/devices/restore", headers=headers, json={"restore_token": token})


def test_delete_without_undo_still_answers_no_content(client, admin_headers, make_device):
    r1, *_ = _lab(make_device)
    assert client.delete(f"/api/devices/{r1.id}", headers=admin_headers).status_code == 204


def test_restore_brings_back_device_login_cables_and_design(client, admin_headers, make_device):
    r1, sw1, sw2 = _lab(make_device)
    r1_id = r1.id
    token = _delete(client, admin_headers, r1_id)
    assert "Secret123!" not in token and "netops" not in token
    assert db.session.get(Device, r1_id) is None
    assert db.session.query(TopologyLink).count() == 0

    response = _restore(client, admin_headers, token)
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["id"] == r1_id
    assert response.get_json()["hostname"] == "R1"
    credential = get_device_credential(r1_id)
    assert (credential.username, credential.password) == ("netops", "Secret123!")
    assert db.session.query(TopologyLink).count() == 2
    assert db.session.query(DesignOspf).filter_by(device_id=r1_id).one().process_id == 7


def test_cable_to_a_device_deleted_since_is_skipped(client, admin_headers, make_device):
    r1, sw1, sw2 = _lab(make_device)
    token = _delete(client, admin_headers, r1.id)
    client.delete(f"/api/devices/{sw2.id}", headers=admin_headers)
    assert _restore(client, admin_headers, token).status_code == 201
    links = db.session.query(TopologyLink).all()
    assert [(l.device_b_id, l.interface_a) for l in links] == [(sw1.id, "Gi0/1")]


def test_restoring_in_reverse_order_brings_back_cables_between_deleted_devices(
    client, admin_headers, make_device
):
    r1, sw1, _ = _lab(make_device)
    first = _delete(client, admin_headers, sw1.id)
    second = _delete(client, admin_headers, r1.id)
    assert _restore(client, admin_headers, second).status_code == 201
    assert _restore(client, admin_headers, first).status_code == 201
    assert db.session.query(TopologyLink).count() == 2


def test_a_token_is_used_once_and_cannot_be_forged(client, admin_headers, make_device):
    r1, *_ = _lab(make_device)
    token = _delete(client, admin_headers, r1.id)
    assert _restore(client, admin_headers, token).status_code == 201
    again = _restore(client, admin_headers, token)
    assert again.status_code == 409
    assert db.session.query(DeviceCredential).count() == 1
    assert _restore(client, admin_headers, token[:-4] + "AAAA").status_code == 422
    assert _restore(client, admin_headers, None).status_code == 422


def test_restore_refuses_when_the_hostname_was_reused(client, admin_headers, make_device):
    r1, *_ = _lab(make_device)
    token = _delete(client, admin_headers, r1.id)
    make_device("R1", "172.16.3.99", "core")
    response = _restore(client, admin_headers, token)
    assert response.status_code == 409
    assert "R1" in response.get_json()["message"]


def test_viewer_cannot_restore(client, admin_headers, viewer_headers, make_device):
    r1, *_ = _lab(make_device)
    token = _delete(client, admin_headers, r1.id)
    assert _restore(client, viewer_headers, token).status_code == 403


def test_restore_keeps_group_membership(client, admin_headers, make_device):
    from network_copilot.groups.model import Group

    r1, *_ = _lab(make_device)
    group = Group(name="Lab A")
    group.devices.append(r1)
    db.session.add(group)
    db.session.commit()
    token = _delete(client, admin_headers, r1.id)
    db.session.expire_all()
    assert db.session.get(Group, group.id).devices == []
    assert _restore(client, admin_headers, token).status_code == 201
    assert [d.hostname for d in db.session.get(Group, group.id).devices] == ["R1"]
