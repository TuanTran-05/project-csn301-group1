from types import SimpleNamespace
from fakes.fake_ssh_client import FakeSSHClient
from network_copilot.changes.capabilities import assess_change
from network_copilot.changes.verification import (
    build_verification_plan,
    is_sensitive_verification_command,
    run_verification,
)


VLAN_BRIEF_WITHOUT_10 = """\
VLAN Name                             Status    Ports
---- -------------------------------- --------- -------------------------------
1    default                          active    Gi0/0, Gi0/1
1002 fddi-default                     act/unsup
1003 token-ring-default               act/unsup
1004 fddinet-default                  act/unsup
1005 trnet-default                    act/unsup
"""


def test_vlan_removal_passes_when_vlan_is_absent():
    assessment = assess_change(["no vlan 10"], "config", "cisco_ios")
    plan = build_verification_plan(assessment, [], SimpleNamespace())
    change = SimpleNamespace(verification_plan=plan, verification_commands=[])

    passed, results = run_verification(
        change,
        FakeSSHClient(default_output=VLAN_BRIEF_WITHOUT_10),
    )

    assert passed is True
    assert results["vlan: 10"]["passed"] is True

def test_sensitive_verification_is_redacted():
    assert is_sensitive_verification_command("show running-config interface Gi0/1")
    plan=[{"id":"generic:show-running-config","label":"show running-config","strategy":"generic","commands":["show running-config"],"required":True,"sensitive":True}]
    passed, result=run_verification(SimpleNamespace(verification_plan=plan,verification_commands=[]),FakeSSHClient(default_output="SECRET"))
    assert passed and result["generic:show-running-config"]["output"]==""


INTERFACE_WITHOUT_DESCRIPTION = """\
Building configuration...

Current configuration : 64 bytes
!
interface GigabitEthernet0/2
 no ip address
 shutdown
 media-type rj45
end
"""

INTERFACE_WITH_DESCRIPTION = INTERFACE_WITHOUT_DESCRIPTION.replace(
    "interface GigabitEthernet0/2\n", "interface GigabitEthernet0/2\n description TEST-LATENCY\n"
)


def _description_check(commands, output):
    assessment = assess_change(commands, "config", "cisco_ios")
    plan = build_verification_plan(assessment, [], SimpleNamespace())
    change = SimpleNamespace(verification_plan=plan, verification_commands=[])
    return run_verification(change, FakeSSHClient(default_output=output))


def test_description_removal_passes_when_the_line_is_gone():
    passed, _ = _description_check(["interface GigabitEthernet0/2", "no description"],
                                   INTERFACE_WITHOUT_DESCRIPTION)
    assert passed is True


def test_description_removal_fails_while_the_line_is_still_there():
    passed, _ = _description_check(["interface GigabitEthernet0/2", "no description"],
                                   INTERFACE_WITH_DESCRIPTION)
    assert passed is False


def test_description_set_passes_when_the_line_is_there():
    passed, _ = _description_check(["interface GigabitEthernet0/2", "description TEST-LATENCY"],
                                   INTERFACE_WITH_DESCRIPTION)
    assert passed is True
