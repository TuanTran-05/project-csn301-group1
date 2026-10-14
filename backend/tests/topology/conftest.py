import pytest

from conftest import _create_device


@pytest.fixture
def lab(app, project):
    """R1 (router), SW1/SW2 (switches) and an ASA in the default project."""
    return {
        "r1": _create_device("R1", "172.16.3.111", "core"),
        "sw1": _create_device("SW1", "172.16.3.121", "access"),
        "sw2": _create_device("SW2", "172.16.3.122", "access"),
        "fw": _create_device("FW1", "172.16.3.10", "firewall", device_type="cisco_asa"),
    }


@pytest.fixture
def base(project):
    return f"/api/projects/{project.id}/topology"


def link_payload(a, if_a, b, if_b, **extra):
    return {
        "device_a_id": a.id,
        "interface_a": if_a,
        "device_b_id": b.id,
        "interface_b": if_b,
        **extra,
    }
