from network_copilot.evaluation.scoring import semantic_commands_match

def test_equivalent_commands_score():
    assert semantic_commands_match([r"^ip route 10\.20\.0\.0 255\.255\.0\.0 10\.10\.10\.1$"],["ip   route 10.20.0.0 255.255.0.0 10.10.10.1"])
    assert semantic_commands_match([r"^show interfaces (?:Gi|GigabitEthernet)0/2 switchport$"],["SHOW   INTERFACES gi0/2 SWITCHPORT"])
