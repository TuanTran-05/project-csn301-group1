from network_copilot.changes.capabilities import assess_change

def test_dhcp_capability_requires_network_and_router():
    good=["ip dhcp pool X","network 192.168.1.0 255.255.255.0","default-router 192.168.1.1"]
    assert assess_change(good,"config","cisco_ios").capability_tier=="level_a_extended"
    assert assess_change(["ip dhcp pool X","network 192.168.1.0 255.255.255.0"],"config","cisco_ios").capability_tier=="best_effort"
