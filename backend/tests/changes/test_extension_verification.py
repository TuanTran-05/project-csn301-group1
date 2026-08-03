from types import SimpleNamespace
from fakes.fake_ssh_client import FakeSSHClient
from network_copilot.changes.verification import run_verification

def test_acl_verification_requires_definition_and_attachment():
    plan=[{"id":"acl:X","label":"IPv4 ACL","strategy":"ipv4_acl","commands":["show access-lists","show running-config interface Gi0/1"],"required":True,"sensitive":True,"expectation":{"family":"ipv4_acl","data":{"name":"X","rules":["permit 10.20.0.0 0.0.255.255","deny any"],"interface":"Gi0/1","direction":"in"}}}]
    client=FakeSSHClient(responses={"show access-lists":"Standard IP access list X\n 10 permit 10.20.0.0, wildcard bits 0.0.255.255\n 20 deny any","show running-config interface Gi0/1":"interface GigabitEthernet0/1\n ip access-group X in"})
    passed, result=run_verification(SimpleNamespace(verification_plan=plan,verification_commands=[]),client)
    assert passed is True and result["acl:X"]["output"]==""

def test_ospf_verification_rejects_wrong_area():
    plan=[{"id":"ospf:10","label":"OSPF","strategy":"single_area_ospf","commands":["show running-config | section ^router ospf"],"required":True,"sensitive":True,"expectation":{"family":"single_area_ospf","data":{"process_id":10,"networks":[{"address":"10.20.0.0","wildcard":"0.0.255.255","area":0}],"passive_interfaces":[]}}}]
    client=FakeSSHClient(responses={"show running-config | section ^router ospf":"router ospf 10\n network 10.20.0.0 0.0.255.255 area 1"})
    passed,_=run_verification(SimpleNamespace(verification_plan=plan,verification_commands=[]),client)
    assert passed is False

def test_dhcp_verification_accepts_zero_leases():
    plan=[{"id":"dhcp:STUDENT","label":"DHCP","strategy":"ios_dhcp_pool","commands":["show ip dhcp pool"],"required":True,"sensitive":False,"expectation":{"family":"ios_dhcp_pool","data":{"pool":"STUDENT","network":"192.168.30.0/24"}}}]
    client=FakeSSHClient(responses={"show ip dhcp pool":"Pool STUDENT\n Network 192.168.30.0/24\n Leased addresses : 0"})
    passed,_=run_verification(SimpleNamespace(verification_plan=plan,verification_commands=[]),client)
    assert passed is True
