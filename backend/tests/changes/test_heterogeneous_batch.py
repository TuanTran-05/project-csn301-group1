from network_copilot.changes import batch_service
from network_copilot.changes.batch_service import BatchOperation


def test_apply_batch_routes_heterogeneous_children_by_execution_mode(
    app, admin_user, access_switch, dist_switch, ssh_factory
):
    """Each frozen child uses its own SSH execution mode before success aggregates."""
    access_client = ssh_factory.set_client(
        access_switch.hostname,
        responses={
            "show running-config": "hostname ACC-SW1",
            "show startup-config": "hostname ACC-SW1",
        },
        exec_output="[OK]",
    )
    dist_client = ssh_factory.set_client(
        dist_switch.hostname,
        responses={
            "show running-config": "hostname DIST-SW1",
            "show startup-config": "hostname DIST-SW1",
            "show vlan brief": (
                "VLAN Name                             Status    Ports\n"
                "---- -------------------------------- --------- ---\n"
                "220  VOICE                            active\n"
            ),
        },
        config_output="DIST-SW1(config)#",
    )
    batch = batch_service.create_batch_preview(
        admin_user.id,
        [
            BatchOperation(["ACC-SW1"], "exec", ["write memory"], []),
            BatchOperation(
                ["DIST-SW1"],
                "config",
                ["configure terminal", "vlan 220", "name VOICE", "end"],
                [],
            ),
        ],
        "Save access config and create the distribution voice VLAN.",
    )
    batch_service.approve_batch(batch.id, admin_user.id)

    result = batch_service.apply_batch(
        batch.id, admin_user.id, confirmation="CONFIRM ALL"
    )

    assert result.status == "success"
    assert {child.device.hostname: child.status for child in result.changes} == {
        "ACC-SW1": "success",
        "DIST-SW1": "success",
    }
    assert access_client.exec_batches == [["write memory"]]
    assert access_client.config_batches == []
    assert dist_client.config_batches == [
        ["configure terminal", "vlan 220", "name VOICE", "end"]
    ]
    assert dist_client.exec_batches == []
