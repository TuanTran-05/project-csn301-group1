import pytest

from network_copilot.changes import batch_service
from network_copilot.changes import service as change_service
from network_copilot.changes.batch_service import BatchOperation
from network_copilot.changes.model import ChangeBatch, ChangeRequest
from network_copilot.errors import NotFoundError, ValidationError


def write_all():
    return BatchOperation(
        device_hostnames=["*"],
        execution_mode="exec",
        commands=["write memory"],
        verification_commands=[],
    )


# -- target resolution and atomicity ---------------------------------------


def test_wildcard_preview_freezes_devices_in_hostname_order(
    app, admin_user, access_switch, dist_switch, core_switch
):
    batch = batch_service.create_batch_preview(
        admin_user.id, [write_all()], "Save every device"
    )
    assert [change.device.hostname for change in batch.changes] == [
        "ACC-SW1", "CORE-SW1", "DIST-SW1"
    ]
    assert all(change.execution_mode == "exec" for change in batch.changes)
    assert batch.requires_confirmation is True
    assert batch.risk_level == "high"


def test_wildcard_snapshot_does_not_gain_later_device(
    app, admin_user, access_switch, make_device
):
    batch = batch_service.create_batch_preview(admin_user.id, [write_all()], "Save")
    make_device("LATE-SW1", "10.10.10.99", "access")
    assert [change.device.hostname for change in batch.changes] == ["ACC-SW1"]


def test_conflicting_duplicate_target_rolls_back_entire_preview(
    admin_user, access_switch, db
):
    operations = [
        BatchOperation(["ACC-SW1"], "exec", ["write memory"], []),
        BatchOperation(["ACC-SW1"], "exec", ["reload"], []),
    ]
    with pytest.raises(ValidationError, match="conflicting"):
        batch_service.create_batch_preview(admin_user.id, operations, "Conflict")
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(ChangeRequest).count() == 0


def test_wildcard_mixed_with_explicit_hostname_is_rejected(
    app, admin_user, access_switch, dist_switch
):
    operation = BatchOperation(["*", "DIST-SW1"], "exec", ["write memory"], [])
    with pytest.raises(ValidationError):
        batch_service.create_batch_preview(admin_user.id, [operation], "Bad")


def test_unknown_hostname_is_rejected_and_writes_nothing(
    app, admin_user, access_switch, db
):
    operation = BatchOperation(["ACC-SW1", "NOPE"], "exec", ["write memory"], [])
    with pytest.raises(ValidationError):
        batch_service.create_batch_preview(admin_user.id, [operation], "Bad")
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(ChangeRequest).count() == 0


def test_no_operations_is_rejected(app, admin_user):
    with pytest.raises(ValidationError):
        batch_service.create_batch_preview(admin_user.id, [], "Empty")


def test_operation_with_no_target_hostnames_is_rejected(
    app, admin_user, access_switch, db
):
    """An empty device_hostnames list is meaningless and must not silently
    resolve to zero devices - it should be rejected as a ValidationError, not
    let a later max() over an empty batch.changes raise a raw ValueError."""
    operation = BatchOperation([], "exec", ["write memory"], [])
    with pytest.raises(ValidationError):
        batch_service.create_batch_preview(admin_user.id, [operation], "Bad")
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(ChangeRequest).count() == 0


def test_wildcard_batch_against_empty_device_table_is_rejected(app, admin_user, db):
    """A wildcard batch run when no devices exist resolves to zero devices;
    this must raise ValidationError, not a raw ValueError from max()."""
    with pytest.raises(ValidationError, match="did not resolve"):
        batch_service.create_batch_preview(admin_user.id, [write_all()], "Save")
    assert db.session.query(ChangeBatch).count() == 0
    assert db.session.query(ChangeRequest).count() == 0


def test_same_device_targeted_twice_by_identical_operations_is_not_a_conflict(
    app, admin_user, access_switch
):
    """Two identical operations that both resolve to the same device are not
    a conflict - they describe the same work, so the second is a no-op."""
    operation = write_all()
    batch = batch_service.create_batch_preview(
        admin_user.id, [operation, operation], "Repeat"
    )
    assert [change.device.hostname for change in batch.changes] == ["ACC-SW1"]


def test_low_risk_batch_does_not_require_confirmation(
    app, admin_user, access_switch, dist_switch
):
    operation = BatchOperation(
        ["ACC-SW1", "DIST-SW1"], "config",
        ["configure terminal", "vlan 25", "name MARKETING", "end"], [],
    )
    batch = batch_service.create_batch_preview(admin_user.id, [operation], "VLAN")
    assert batch.requires_confirmation is False
    assert batch.risk_level == "medium"  # DIST-SW1 is a distribution device


# -- get_batch --------------------------------------------------------------


def test_get_batch_returns_the_batch_with_children(app, admin_user, access_switch):
    created = batch_service.create_batch_preview(admin_user.id, [write_all()], "Save")
    fetched = batch_service.get_batch(created.id)
    assert fetched.id == created.id
    assert [c.device.hostname for c in fetched.changes] == ["ACC-SW1"]


def test_get_unknown_batch_raises_not_found(app):
    with pytest.raises(NotFoundError, match="999"):
        batch_service.get_batch(999)


# -- list_batches -------------------------------------------------------------


def test_list_batches_orders_newest_first(app, admin_user, access_switch, dist_switch):
    first = batch_service.create_batch_preview(
        admin_user.id, [BatchOperation(["ACC-SW1"], "exec", ["write memory"], [])], "First"
    )
    second = batch_service.create_batch_preview(
        admin_user.id, [BatchOperation(["DIST-SW1"], "exec", ["write memory"], [])], "Second"
    )
    batches = batch_service.list_batches()
    assert [b.id for b in batches] == [second.id, first.id]


def test_list_batches_bounds_the_limit(app, admin_user, access_switch):
    for _ in range(3):
        batch_service.create_batch_preview(
            admin_user.id, [BatchOperation(["ACC-SW1"], "exec", ["write memory"], [])], "x"
        )
    assert len(batch_service.list_batches(limit=2)) == 2
    assert len(batch_service.list_batches(limit=0)) == 1  # floors at 1


# -- list_changes(standalone_only=...) ---------------------------------------


def test_list_changes_standalone_only_excludes_batch_children(
    app, admin_user, access_switch, dist_switch
):
    batch_service.create_batch_preview(
        admin_user.id,
        [BatchOperation(["ACC-SW1"], "exec", ["write memory"], [])],
        "Batch",
    )
    change_service.create_preview(
        admin_user.id,
        device_id=dist_switch.id,
        commands=["configure terminal", "vlan 25", "name MARKETING", "end"],
    )

    all_changes = change_service.list_changes()
    assert len(all_changes) == 2

    standalone = change_service.list_changes(standalone_only=True)
    assert len(standalone) == 1
    assert standalone[0].batch_id is None
    assert standalone[0].device_id == dist_switch.id
