import pytest

from network_copilot.changes import service as change_service
from network_copilot.changes.capabilities import assess_change, recognize_change


def test_preview_freezes_capability_metadata(app, admin_user, access_switch):
    change = change_service.create_preview(
        admin_user.id,
        access_switch.id,
        ["interface Gi0/2", "description STUDENT"],
    )

    payload = change.to_dict()
    assert payload["capability_tier"] == "level_a_core"
    assert payload["verification_level"] == "semantic"
    assert payload["operation_families"] == ["interface_description"]
    assert payload["verification_plan"][0]["strategy"] == "interface_description"
    assert payload["rollback_guidance"]


def test_initial_assessment_is_best_effort(app):
    assessment = assess_change(
        ["hostname LAB-SW"], "config", "cisco_ios"
    )
    assert assessment.capability_tier == "best_effort"
    assert assessment.verification_level == "best_effort"
    assert assessment.expectations == ()


@pytest.mark.parametrize(
    ("commands", "mode", "families"),
    [
        (["vlan 30", "name STUDENT"], "config", ["vlan"]),
        (
            [
                "interface Gi0/2",
                "switchport mode access",
                "switchport access vlan 30",
            ],
            "config",
            ["access_port"],
        ),
        (
            [
                "interface Gi0/1",
                "switchport mode trunk",
                "switchport trunk allowed vlan 10,20,30",
            ],
            "config",
            ["trunk_port"],
        ),
        (
            ["interface Gi0/2", "description STUDENT"],
            "config",
            ["interface_description"],
        ),
        (["interface Gi0/2", "no shutdown"], "config", ["interface_admin_state"]),
        (
            ["interface Gi0/1", "ip address 10.20.1.1 255.255.255.0"],
            "config",
            ["interface_ipv4"],
        ),
        (
            ["ip route 10.20.0.0 255.255.0.0 10.10.10.1"],
            "config",
            ["static_route"],
        ),
        (
            ["copy running-config startup-config"],
            "exec",
            ["save_config"],
        ),
    ],
)
def test_recognizes_level_a_core(commands, mode, families):
    expectations, unmatched = recognize_change(commands, mode)
    assert unmatched is False
    assert list(dict.fromkeys(item.family for item in expectations)) == families


def test_recognition_is_case_insensitive_but_preserves_values():
    expectations, unmatched = recognize_change(
        ["Vlan 30", "NAME Student_Lab"], "config"
    )
    assert unmatched is False
    assert expectations[0].data["name"] == "Student_Lab"

    expectations, unmatched = recognize_change(
        ["INTERFACE gi0/2", "No Shutdown"], "config"
    )
    assert unmatched is False
    assert expectations[0].data == {
        "interface": "gi0/2",
        "enabled": True,
    }

    expectations, unmatched = recognize_change(["WRITE MEMORY"], "exec")
    assert unmatched is False
    assert expectations[0].family == "save_config"


@pytest.mark.parametrize(
    "commands",
    [
        ["interface Gi0/1", "ip address 10.20.1.1 255.0.255.0"],
        ["interface Gi0/1", "ip address 10.20.1.1 255.255.255.0 secondary"],
        ["interface range Gi0/1 - 3", "shutdown"],
        ["interface Gi0/1", "switchport mode trunk", "switchport trunk allowed vlan add 30"],
        ["ip route 10.20.0.0 255.255.0.0 Ethernet0/1"],
        ["hostname LAB-SW"],
        ["vlan 30", "interface Gi0/1"],
    ],
)
def test_out_of_catalogue_sequences_are_unmatched(commands):
    expectations, unmatched = recognize_change(commands, "config")
    assert expectations == ()
    assert unmatched is True


@pytest.mark.parametrize(
    "commands",
    [
        ["vlan 4095"],
        ["vlan 0"],
        ["interface Gi0/1", "switchport access vlan 4095"],
    ],
)
def test_invalid_vlan_ranges_are_best_effort_without_raising(app, commands):
    assessment = assess_change(commands, "config", "cisco_ios")
    assert assessment.capability_tier == "best_effort"
    assert assessment.verification_level == "best_effort"


@pytest.mark.parametrize(
    "commands",
    [
        ["vlan 30"],
        ["interface Gi0/1", "switchport mode access", "switchport access vlan 30"],
        ["interface Gi0/1", "ip address 10.20.1.1 255.255.255.0"],
    ],
)
def test_only_vlan_is_semantically_enabled_at_this_checkpoint(commands):
    assessment = assess_change(commands, "config", "cisco_ios")
    if commands == ["vlan 30"]:
        assert assessment.capability_tier == "level_a_core"
        assert assessment.verification_level == "semantic"
    elif commands == ["interface Gi0/1", "switchport mode access", "switchport access vlan 30"]:
        assert assessment.capability_tier == "level_a_core"
        assert assessment.verification_level == "semantic"
    else:
        assert assessment.capability_tier == "level_a_core"
        assert assessment.verification_level == "semantic"


def test_asa_is_never_semantic_even_for_recognized_commands():
    assessment = assess_change(["vlan 30"], "config", "cisco_asa")
    assert assessment.capability_tier == "best_effort"
    assert assessment.verification_level == "best_effort"
