from network_copilot.parsers.neighbors import parse_cdp_detail, parse_lldp_detail

CDP = """-------------------------
Device ID: SW1.lab.local
Entry address(es):
  IP address: 172.16.3.121
Platform: cisco , Capabilities: Router Switch IGMP
Interface: GigabitEthernet0/1,  Port ID (outgoing port): GigabitEthernet0/0
Holdtime : 148 sec

-------------------------
Device ID: ISP-ROUTER
Entry address(es):
Platform: Linux,  Capabilities: Router
Interface: GigabitEthernet0/2,  Port ID (outgoing port): eth0
Holdtime : 120 sec
"""

LLDP = """------------------------------------------------
Local Intf: Gi0/1
Chassis id: aabb.cc00.0100
Port id: Gi0/0
Port Description: GigabitEthernet0/0
System Name: SW2.lab.local

System Description:
Cisco IOS Software
Management Addresses:
    IP: 172.16.3.122
------------------------------------------------
Local Intf: Gi0/3
Port id: Gi0/9
"""


def test_cdp_detail_is_parsed():
    result = parse_cdp_detail(CDP)
    assert result[0] == {
        "protocol": "cdp", "neighbor": "SW1.lab.local",
        "local_interface": "GigabitEthernet0/1", "remote_interface": "GigabitEthernet0/0",
        "ip": "172.16.3.121", "platform": "cisco",
    }
    assert result[1]["neighbor"] == "ISP-ROUTER" and result[1]["ip"] is None


def test_lldp_detail_keeps_only_complete_entries():
    result = parse_lldp_detail(LLDP)
    assert len(result) == 1  # the second entry has no system name
    assert (result[0]["neighbor"], result[0]["local_interface"], result[0]["remote_interface"]) == (
        "SW2.lab.local", "Gi0/1", "Gi0/0")
    assert result[0]["ip"] == "172.16.3.122"


def test_empty_or_unrelated_output_yields_nothing():
    assert parse_cdp_detail("") == []
    assert parse_cdp_detail("% CDP is not enabled") == []
    assert parse_lldp_detail("% LLDP is not enabled") == []
