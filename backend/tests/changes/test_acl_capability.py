from network_copilot.changes.capabilities import assess_change

def test_acl_capability_is_extended_only_for_bounded_shape():
    commands=["ip access-list standard X","permit any","interface Gi0/1","ip access-group X in"]
    assert assess_change(commands,"config","cisco_ios").capability_tier=="level_a_extended"
    assert assess_change(["access-list 101 permit tcp any any"],"config","cisco_ios").capability_tier=="best_effort"
