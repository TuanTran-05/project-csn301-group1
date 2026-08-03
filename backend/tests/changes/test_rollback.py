from network_copilot.changes.capabilities import assess_change
from network_copilot.changes.rollback import build_rollback_guidance

def test_rollback_is_guidance_only_for_extensions():
    assessment=assess_change(["router ospf 10","network 10.0.0.0 0.0.0.255 area 0"],"config","cisco_ios")
    guidance=build_rollback_guidance(assessment,[])
    assert guidance and "backup" in guidance[0].lower()
