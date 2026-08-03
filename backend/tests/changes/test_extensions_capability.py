from network_copilot.changes.capabilities import assess_change
from network_copilot.changes import service as change_service

def test_bounded_standard_acl_is_extended():
    assessment=assess_change(["ip access-list standard STUDENT_IN","permit 10.20.0.0 0.0.255.255","deny any","interface Gi0/1","ip access-group STUDENT_IN in"],"config","cisco_ios")
    assert assessment.capability_tier=="level_a_extended"
    assert assessment.operation_families==("ipv4_acl",)

def test_bounded_dhcp_is_extended():
    assessment=assess_change(["ip dhcp excluded-address 192.168.30.1 192.168.30.20","ip dhcp pool STUDENT","network 192.168.30.0 255.255.255.0","default-router 192.168.30.1","dns-server 1.1.1.1 8.8.8.8"],"config","cisco_ios")
    assert assessment.capability_tier=="level_a_extended"
    assert assessment.operation_families==("ios_dhcp_pool",)

def test_bounded_single_area_ospf_is_extended():
    assessment=assess_change(["router ospf 10","router-id 10.255.0.1","network 10.20.0.0 0.0.255.255 area 0","passive-interface Gi0/3"],"config","cisco_ios")
    assert assessment.capability_tier=="level_a_extended"
    assert assessment.operation_families==("single_area_ospf",)

def test_out_of_scope_ospf_stays_best_effort():
    assessment=assess_change(["router ospf 10","network 10.20.0.0 0.0.255.255 area 1"],"config","cisco_ios")
    assert assessment.capability_tier=="best_effort"

def test_acl_attachment_is_confirmation_gated(app, admin_user, core_switch):
    change=change_service.create_preview(admin_user.id, core_switch.id, ["ip access-list standard X","permit any","interface Gi0/1","ip access-group X in"])
    assert change.requires_confirmation is True
