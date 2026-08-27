from scripts import seed_lab
from network_copilot.devices.model import Device


EXPECTED_DEVICES = [
    ("R1", "172.16.3.111", "cisco_ios", "core"),
    ("SW1", "172.16.3.121", "cisco_ios", "access"),
    ("SW2", "172.16.3.122", "cisco_ios", "access"),
]


def rows():
    devices = Device.query.order_by(Device.hostname).all()
    return [
        (item.hostname, item.management_ip, item.device_type, item.role)
        for item in devices
    ]


def test_lab_device_manifest_matches_current_pnetlab_topology():
    assert seed_lab.LAB_DEVICES == EXPECTED_DEVICES


def test_seed_devices_creates_exact_inventory(app):
    created, updated = seed_lab.seed_devices()
    assert (created, updated) == (3, 0)
    assert rows() == EXPECTED_DEVICES


def test_seed_devices_is_idempotent(app):
    seed_lab.seed_devices()
    created, updated = seed_lab.seed_devices()
    assert (created, updated) == (0, 3)
    assert rows() == EXPECTED_DEVICES
