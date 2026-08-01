from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.extensions import db


def test_batch_serializes_children_and_confirmation_text(
    app, admin_user, access_switch, dist_switch
):
    batch = ChangeBatch(
        status="pending_approval",
        risk_level="high",
        requires_confirmation=True,
        requested_by_id=admin_user.id,
        description="Save all configurations",
        source="ai",
    )
    batch.changes = [
        ChangeRequest(device_id=access_switch.id, commands=["write memory"], execution_mode="exec"),
        ChangeRequest(device_id=dist_switch.id, commands=["write memory"], execution_mode="exec"),
    ]
    db.session.add(batch)
    db.session.commit()

    payload = batch.to_dict()
    assert payload["confirmation_text"] == "CONFIRM ALL"
    assert [item["device"]["hostname"] for item in payload["changes"]] == [
        "ACC-SW1",
        "DIST-SW1",
    ]
    assert all(item["execution_mode"] == "exec" for item in payload["changes"])


def test_single_child_batch_uses_hostname_confirmation(app, admin_user, access_switch):
    batch = ChangeBatch(status="pending_approval", risk_level="high", requires_confirmation=True)
    batch.changes = [ChangeRequest(device_id=access_switch.id, commands=["reload"], execution_mode="exec")]
    db.session.add(batch)
    db.session.commit()
    assert batch.to_dict()["confirmation_text"] == "ACC-SW1"


def test_batch_serializes_children_sorted_by_hostname(app, admin_user, access_switch, dist_switch):
    """Verify that children are explicitly sorted by hostname, not insertion order."""
    batch = ChangeBatch(
        status="pending_approval",
        risk_level="high",
        requires_confirmation=True,
        requested_by_id=admin_user.id,
    )
    # Add children in REVERSE alphabetical order by hostname (DIST-SW1 before ACC-SW1)
    batch.changes = [
        ChangeRequest(device_id=dist_switch.id, commands=["write memory"], execution_mode="exec"),
        ChangeRequest(device_id=access_switch.id, commands=["write memory"], execution_mode="exec"),
    ]
    db.session.add(batch)
    db.session.commit()

    payload = batch.to_dict()
    # Verify serialization is sorted by hostname despite insertion order
    assert [item["device"]["hostname"] for item in payload["changes"]] == [
        "ACC-SW1",
        "DIST-SW1",
    ]
