from network_copilot.parsers import parse_ip_dhcp_pool

def test_dhcp_pool_parser_normalizes_network_and_counts():
    row=parse_ip_dhcp_pool("Pool STUDENT\n Network 192.168.30.0 255.255.255.0\n Leased addresses : 2\n Excluded addresses : 10\n Total addresses : 254")[0]
    assert row["name"]=="STUDENT" and row["network"]=="192.168.30.0/24"
    assert row["leased"]==2 and row["excluded"]==10 and row["total"]==254
