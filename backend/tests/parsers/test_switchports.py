from network_copilot.parsers.switchports import normalize_interface_name, normalize_vlan_set, parse_switchport_detail, parse_interfaces_trunk

def test_switchport_aliases_and_vlan_ranges():
    assert normalize_interface_name("Gi0/1") == "GigabitEthernet0/1"
    assert normalize_interface_name("gi0/1") == "GigabitEthernet0/1"
    assert normalize_vlan_set("10,20,30-32") == [10,20,30,31,32]

def test_switchport_and_trunk_parsers():
    detail=parse_switchport_detail("Name: Gi0/1\nAdministrative Mode: trunk\nTrunking VLANs Enabled: 10,20,30")
    assert detail[0]["allowed_vlans"] == [10,20,30]
    trunk=parse_interfaces_trunk("Gi0/1 on 802.1q trunking 1 10,20,30")
    assert trunk[0]["status"] == "trunking"
