from network_copilot.parsers import parse_access_lists, parse_ip_dhcp_pool

def test_parse_standard_acl():
    rows=parse_access_lists("Standard IP access list STUDENT_IN\n    10 permit 10.20.0.0, wildcard bits 0.0.255.255\n    20 deny any")
    assert rows[0]["rules"][0]["wildcard"]=="0.0.255.255"
    assert rows[0]["rules"][1]["source"]=="any"

def test_parse_dhcp_pool_network_and_counters():
    row=parse_ip_dhcp_pool("Pool STUDENT\n  Network 192.168.30.0/24\n  Leased addresses : 2\n  Excluded addresses : 10\n  Total addresses : 254")[0]
    assert row["network"]=="192.168.30.0/24"
    assert row["leased"]==2 and row["excluded"]==10 and row["total"]==254
