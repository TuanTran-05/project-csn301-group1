from types import SimpleNamespace

from scripts import smoke_test_lab


def devices(*hostnames):
    return [SimpleNamespace(hostname=hostname) for hostname in hostnames]


def test_inventory_error_accepts_exact_current_inventory():
    assert smoke_test_lab.inventory_error(devices("R1", "SW1", "SW2")) is None


def test_inventory_error_rejects_missing_device():
    error = smoke_test_lab.inventory_error(devices("R1", "SW1"))
    assert error == "inventory mismatch: missing=['SW2'], unexpected=[]"


def test_inventory_error_rejects_retired_device():
    error = smoke_test_lab.inventory_error(
        devices("R1", "SW1", "SW2", "INTERNAL-RTR")
    )
    assert error == "inventory mismatch: missing=[], unexpected=['INTERNAL-RTR']"
