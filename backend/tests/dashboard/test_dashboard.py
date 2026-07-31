from network_copilot.audit import service as audit_service
from network_copilot.changes import service as change_service
from network_copilot.dashboard.service import build_summary
from network_copilot.extensions import db
from network_copilot.monitoring.model import DeviceSnapshot

FULL_NEIGHBOR = {
    "neighbor_id": "3.3.3.3",
    "priority": 1,
    "state": "FULL/DR",
    "dead_time": "00:00:33",
    "address": "10.255.0.6",
    "interface": "GigabitEthernet0/1",
}

NOT_FULL_NEIGHBOR = {
    "neighbor_id": "4.4.4.4",
    "priority": 1,
    "state": "2WAY/DROTHER",
    "dead_time": "00:00:33",
    "address": "10.255.0.7",
    "interface": "GigabitEthernet0/2",
}


def _snapshot(device, status="online", parsed_data=None):
    snapshot = DeviceSnapshot(
        device_id=device.id,
        status=status,
        raw_output={},
        parsed_data=parsed_data if parsed_data is not None else {},
    )
    db.session.add(snapshot)
    db.session.commit()
    return snapshot


# -- device role rollup -----------------------------------------------------


def test_device_role_rollup_counts_by_status(app, make_device):
    core = make_device("CORE1", "10.0.0.1", "core")
    core.status = "online"
    dist_online = make_device("DIST1", "10.0.0.2", "distribution")
    dist_online.status = "online"
    dist_offline = make_device("DIST2", "10.0.0.3", "distribution")
    dist_offline.status = "offline"
    db.session.commit()

    summary = build_summary()

    assert summary["devices"]["by_role"] == {
        "core": {"total": 1, "online": 1, "offline": 0, "unknown": 0},
        "distribution": {"total": 2, "online": 1, "offline": 1, "unknown": 0},
    }


def test_device_role_rollup_defaults_to_unknown(app, make_device):
    make_device("ACC1", "10.0.0.4", "access")

    summary = build_summary()

    assert summary["devices"]["by_role"]["access"] == {
        "total": 1,
        "online": 0,
        "offline": 0,
        "unknown": 1,
    }


# -- OSPF panel membership ---------------------------------------------------


def test_ospf_panel_only_includes_core_and_distribution(app, make_device):
    make_device("ACC1", "10.0.0.5", "access")
    make_device("CORE1", "10.0.0.6", "core")

    summary = build_summary()

    hostnames = [entry["hostname"] for entry in summary["ospf"]]
    assert hostnames == ["CORE1"]


# -- OSPF health classification ----------------------------------------------


def test_ospf_health_is_no_data_without_a_snapshot(app, make_device):
    make_device("CORE1", "10.0.0.7", "core")

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "no_data"
    assert entry["neighbor_count"] == 0
    assert entry["snapshot_at"] is None


def test_ospf_health_is_no_data_when_the_device_is_offline(app, make_device):
    device = make_device("CORE1", "10.0.0.8", "core")
    _snapshot(device, status="offline", parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "no_data"


def test_ospf_health_is_no_data_when_snapshot_has_no_ospf_key(app, make_device):
    device = make_device("CORE1", "10.0.0.9", "core")
    _snapshot(device, parsed_data={"show ip route": []})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "no_data"


def test_ospf_health_is_down_with_zero_neighbors(app, make_device):
    device = make_device("CORE1", "10.0.0.10", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": []})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "down"


def test_ospf_health_is_degraded_with_a_non_full_neighbor(app, make_device):
    device = make_device("CORE1", "10.0.0.11", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [NOT_FULL_NEIGHBOR]})

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "degraded"
    assert entry["neighbor_count"] == 1
    assert entry["full_count"] == 0


def test_ospf_health_is_ok_when_all_neighbors_are_full(app, make_device):
    device = make_device("CORE1", "10.0.0.12", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    entry = summary["ospf"][0]
    assert entry["health"] == "ok"
    assert entry["full_count"] == 1
    assert entry["neighbors"][0]["neighbor_id"] == "3.3.3.3"


def test_ospf_uses_the_most_recent_snapshot(app, make_device):
    device = make_device("CORE1", "10.0.0.13", "core")
    _snapshot(device, parsed_data={"show ip ospf neighbor": [NOT_FULL_NEIGHBOR]})
    _snapshot(device, parsed_data={"show ip ospf neighbor": [FULL_NEIGHBOR]})

    summary = build_summary()

    assert summary["ospf"][0]["health"] == "ok"


# -- changes passthrough ------------------------------------------------------


def test_changes_pending_approval_bucket(app, admin_user, access_switch):
    change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=["show version"]
    )

    summary = build_summary()

    assert len(summary["changes"]["pending_approval"]) == 1
    assert (
        summary["changes"]["pending_approval"][0]["device"]["hostname"]
        == "ACC-SW1"
    )


def test_changes_recent_bucket_includes_every_status(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id, device_id=access_switch.id, commands=["show version"]
    )
    change_service.cancel(change.id, admin_user.id)

    summary = build_summary()

    assert len(summary["changes"]["recent"]) == 1
    assert summary["changes"]["recent"][0]["status"] == "cancelled"


# -- audit passthrough --------------------------------------------------------


def test_audit_recent_bucket(app):
    audit_service.record_event("device.refresh", "success", message="ok")

    summary = build_summary()

    assert len(summary["audit"]["recent"]) == 1
    assert summary["audit"]["recent"][0]["action"] == "device.refresh"


# -- generated_at --------------------------------------------------------------


def test_generated_at_is_present(app):
    summary = build_summary()
    assert summary["generated_at"]


# -- API -----------------------------------------------------------------


def test_summary_endpoint_requires_authentication(client):
    assert client.get("/api/dashboard/summary").status_code == 401


def test_summary_endpoint_is_readable_by_viewer(client, viewer_headers):
    response = client.get("/api/dashboard/summary", headers=viewer_headers)
    assert response.status_code == 200
    body = response.get_json()
    assert "devices" in body
    assert "ospf" in body
    assert "changes" in body
    assert "audit" in body
    assert "generated_at" in body


def test_dashboard_page_is_served(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
