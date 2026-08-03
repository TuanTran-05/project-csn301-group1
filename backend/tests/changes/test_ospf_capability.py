from network_copilot.changes.capabilities import assess_change

def test_ospf_area_zero_subset_only():
    good=["router ospf 10","network 10.20.0.0 0.0.255.255 area 0"]
    assert assess_change(good,"config","cisco_ios").capability_tier=="level_a_extended"
    assert assess_change(["router ospf 10","network 10.20.0.0 0.0.255.255 area 1"],"config","cisco_ios").capability_tier=="best_effort"
