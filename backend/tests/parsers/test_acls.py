from network_copilot.parsers import parse_access_lists

def test_standard_acl_fixture():
    rows=parse_access_lists("Standard IP access list STUDENT_IN\n 10 permit 10.20.0.0, wildcard bits 0.0.255.255\n 20 deny any")
    assert rows == [{"name":"STUDENT_IN","type":"standard","rules":[{"sequence":10,"action":"permit","source":"10.20.0.0","wildcard":"0.0.255.255"},{"sequence":20,"action":"deny","source":"any","wildcard":None}]}]
