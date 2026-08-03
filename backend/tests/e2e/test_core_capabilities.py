from network_copilot.changes.capabilities import assess_change

def test_core_families_freeze_as_level_a_core():
    cases=[(["vlan 30"],"vlan"),(["interface Gi0/1","switchport mode access","switchport access vlan 30"],"access_port"),(["ip route 10.20.0.0 255.255.0.0 10.10.10.1"],"static_route")]
    for commands,family in cases:
        assessment=assess_change(commands,"config","cisco_ios")
        assert assessment.capability_tier=="level_a_core"
        assert assessment.operation_families==(family,)
